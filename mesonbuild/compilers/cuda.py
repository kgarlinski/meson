# Copyright 2012-2017 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import enum
import os.path
import typing as T

from .. import coredata
from .. import mlog
from ..mesonlib import EnvironmentException, MachineChoice, Popen_safe, OptionOverrideProxy, is_windows, LibType
from .compilers import (Compiler, cuda_buildtype_args, cuda_optimization_args,
                        cuda_debug_args)

if T.TYPE_CHECKING:
    from ..build import BuildTarget
    from ..coredata import OptionDictType
    from ..dependencies import Dependency, ExternalProgram
    from ..environment import Environment  # noqa: F401
    from ..envconfig import MachineInfo
    from ..linkers import DynamicLinker


class _Phase(enum.Enum):

    COMPILER = 'compiler'
    LINKER = 'linker'


class CudaCompiler(Compiler):

    LINKER_PREFIX = '-Xlinker='
    language = 'cuda'

    _universal_flags = {_Phase.COMPILER: ['-I', '-D', '-U', '-E'], _Phase.LINKER: ['-l', '-L']}  # type: T.Dict[_Phase, T.List[str]]

    def __init__(self, exelist: T.List[str], version: str, for_machine: MachineChoice,
                 is_cross: bool, exe_wrapper: T.Optional['ExternalProgram'],
                 host_compiler: Compiler, info: 'MachineInfo',
                 linker: T.Optional['DynamicLinker'] = None,
                 full_version: T.Optional[str] = None):
        super().__init__(exelist, version, for_machine, info, linker=linker, full_version=full_version, is_cross=is_cross)
        self.exe_wrapper = exe_wrapper
        self.host_compiler = host_compiler
        self.base_options = host_compiler.base_options
        self.id = 'nvcc'
        self.warn_args = {level: self._to_host_flags(flags) for level, flags in host_compiler.warn_args.items()}

    @classmethod
    def _to_host_flags(cls, flags: T.List[str], phase: _Phase = _Phase.COMPILER) -> T.List[str]:
        return [cls._to_host_flag(f, phase=phase) for f in flags]

    @classmethod
    def _to_host_flag(cls, flag: str, phase: _Phase) -> str:
        if not flag[0] in ['-', '/'] or flag[:2] in cls._universal_flags[phase]:
            return flag

        return '-X{}={}'.format(phase.value, flag)

    def needs_static_linker(self) -> bool:
        return False

    def thread_link_flags(self, environment: 'Environment') -> T.List[str]:
        return self._to_host_flags(self.host_compiler.thread_link_flags(environment))

    def sanity_check(self, work_dir: str, environment: 'Environment') -> None:
        mlog.debug('Sanity testing ' + self.get_display_language() + ' compiler:', ' '.join(self.exelist))
        mlog.debug('Is cross compiler: %s.' % str(self.is_cross))

        sname = 'sanitycheckcuda.cu'
        code = r'''
        #include <cuda_runtime.h>
        #include <stdio.h>

        __global__ void kernel (void) {}

        int main(void){
            struct cudaDeviceProp prop;
            int count, i;
            cudaError_t ret = cudaGetDeviceCount(&count);
            if(ret != cudaSuccess){
                fprintf(stderr, "%d\n", (int)ret);
            }else{
                for(i=0;i<count;i++){
                    if(cudaGetDeviceProperties(&prop, i) == cudaSuccess){
                        fprintf(stdout, "%d.%d\n", prop.major, prop.minor);
                    }
                }
            }
            fflush(stderr);
            fflush(stdout);
            return 0;
        }
        '''
        binname = sname.rsplit('.', 1)[0]
        binname += '_cross' if self.is_cross else ''
        source_name = os.path.join(work_dir, sname)
        binary_name = os.path.join(work_dir, binname + '.exe')
        with open(source_name, 'w') as ofile:
            ofile.write(code)

        # The Sanity Test for CUDA language will serve as both a sanity test
        # and a native-build GPU architecture detection test, useful later.
        #
        # For this second purpose, NVCC has very handy flags, --run and
        # --run-args, that allow one to run an application with the
        # environment set up properly. Of course, this only works for native
        # builds; For cross builds we must still use the exe_wrapper (if any).
        self.detected_cc = ''
        flags = ['-w', '-cudart', 'static', source_name]
        if self.is_cross and self.exe_wrapper is None:
            # Linking cross built apps is painful. You can't really
            # tell if you should use -nostdlib or not and for example
            # on OSX the compiler binary is the same but you need
            # a ton of compiler flags to differentiate between
            # arm and x86_64. So just compile.
            flags += self.get_compile_only_args()
        flags += self.get_output_args(binary_name)

        # Compile sanity check
        cmdlist = self.exelist + flags
        mlog.debug('Sanity check compiler command line: ', ' '.join(cmdlist))
        pc, stdo, stde = Popen_safe(cmdlist, cwd=work_dir)
        mlog.debug('Sanity check compile stdout: ')
        mlog.debug(stdo)
        mlog.debug('-----\nSanity check compile stderr:')
        mlog.debug(stde)
        mlog.debug('-----')
        if pc.returncode != 0:
            raise EnvironmentException('Compiler {0} can not compile programs.'.format(self.name_string()))

        # Run sanity check (if possible)
        if self.is_cross:
            if self.exe_wrapper is None:
                return
            else:
                cmdlist = self.exe_wrapper.get_command() + [binary_name]
        else:
            cmdlist = self.exelist + ['--run', '"' + binary_name + '"']
        mlog.debug('Sanity check run command line: ', ' '.join(cmdlist))
        pe, stdo, stde = Popen_safe(cmdlist, cwd=work_dir)
        mlog.debug('Sanity check run stdout: ')
        mlog.debug(stdo)
        mlog.debug('-----\nSanity check run stderr:')
        mlog.debug(stde)
        mlog.debug('-----')
        pe.wait()
        if pe.returncode != 0:
            raise EnvironmentException('Executables created by {0} compiler {1} are not runnable.'.format(self.language, self.name_string()))

        # Interpret the result of the sanity test.
        # As mentioned above, it is not only a sanity test but also a GPU
        # architecture detection test.
        if stde == '':
            self.detected_cc = stdo
        else:
            mlog.debug('cudaGetDeviceCount() returned ' + stde)

    def has_header_symbol(self, hname: str, symbol: str, prefix: str,
                          env: 'Environment', *,
                          extra_args: T.Optional[T.List[str]] = None,
                          dependencies: T.Optional[T.List['Dependency']] = None) -> T.Tuple[bool, bool]:
        result, cached = super().has_header_symbol(hname, symbol, prefix, env, extra_args=extra_args, dependencies=dependencies)
        if result:
            return True, cached
        if extra_args is None:
            extra_args = []
        fargs = {'prefix': prefix, 'header': hname, 'symbol': symbol}
        t = '''{prefix}
        #include <{header}>
        using {symbol};
        int main(void) {{ return 0; }}'''
        return self.compiles(t.format(**fargs), env, extra_args=extra_args, dependencies=dependencies)

    def get_options(self) -> 'OptionDictType':
        opts = super().get_options()
        opts.update({'cuda_std': coredata.UserComboOption('C++ language standard to use',
                                                          ['none', 'c++03', 'c++11', 'c++14'],
                                                          'none')})
        return opts

    def _to_host_compiler_options(self, options: 'OptionDictType') -> 'OptionDictType':
        overrides = {name: opt.value for name, opt in options.items()}
        return OptionOverrideProxy(overrides, self.host_compiler.get_options())

    def get_option_compile_args(self, options: 'OptionDictType') -> T.List[str]:
        args = []
        # On Windows, the version of the C++ standard used by nvcc is dictated by
        # the combination of CUDA version and MSVC version; the --std= is thus ignored
        # and attempting to use it will result in a warning: https://stackoverflow.com/a/51272091/741027
        if not is_windows():
            std = options['cuda_std']
            if std.value != 'none':
                args.append('--std=' + std.value)

        return args + self._to_host_flags(self.host_compiler.get_option_compile_args(self._to_host_compiler_options(options)))

    @classmethod
    def _cook_link_args(cls, args: T.List[str]) -> T.List[str]:
        # Prepare link args for nvcc
        cooked = []  # type: T.List[str]
        for arg in args:
            if arg.startswith('-Wl,'): # strip GNU-style -Wl prefix
                arg = arg.replace('-Wl,', '', 1)
            arg = arg.replace(' ', '\\') # espace whitespace
            cooked.append(arg)
        return cls._to_host_flags(cooked, _Phase.LINKER)

    def get_option_link_args(self, options: 'OptionDictType') -> T.List[str]:
        return self._cook_link_args(self.host_compiler.get_option_link_args(self._to_host_compiler_options(options)))

    def get_soname_args(self, env: 'Environment', prefix: str, shlib_name: str,
                        suffix: str, soversion: str,
                        darwin_versions: T.Tuple[str, str],
                        is_shared_module: bool) -> T.List[str]:
        return self._cook_link_args(self.host_compiler.get_soname_args(
            env, prefix, shlib_name, suffix, soversion, darwin_versions,
            is_shared_module))

    def get_compile_only_args(self) -> T.List[str]:
        return ['-c']

    def get_no_optimization_args(self) -> T.List[str]:
        return ['-O0']

    def get_optimization_args(self, optimization_level: str) -> T.List[str]:
        # alternatively, consider simply redirecting this to the host compiler, which would
        # give us more control over options like "optimize for space" (which nvcc doesn't support):
        # return self._to_host_flags(self.host_compiler.get_optimization_args(optimization_level))
        return cuda_optimization_args[optimization_level]

    def get_debug_args(self, is_debug: bool) -> T.List[str]:
        return cuda_debug_args[is_debug]

    def get_werror_args(self) -> T.List[str]:
        return ['-Werror=cross-execution-space-call,deprecated-declarations,reorder']

    def get_warn_args(self, level: str) -> T.List[str]:
        return self.warn_args[level]

    def get_buildtype_args(self, buildtype: str) -> T.List[str]:
        # nvcc doesn't support msvc's "Edit and Continue" PDB format; "downgrade" to
        # a regular PDB to avoid cl's warning to that effect (D9025 : overriding '/ZI' with '/Zi')
        host_args = ['/Zi' if arg == '/ZI' else arg for arg in self.host_compiler.get_buildtype_args(buildtype)]
        return cuda_buildtype_args[buildtype] + self._to_host_flags(host_args)

    def get_include_args(self, path: str, is_system: bool) -> T.List[str]:
        if path == '':
            path = '.'
        return ['-I' + path]

    def get_compile_debugfile_args(self, rel_obj: str, pch: bool = False) -> T.List[str]:
        return self._to_host_flags(self.host_compiler.get_compile_debugfile_args(rel_obj, pch))

    def get_link_debugfile_args(self, targetfile: str) -> T.List[str]:
        return self._cook_link_args(self.host_compiler.get_link_debugfile_args(targetfile))

    def get_depfile_suffix(self) -> str:
        return 'd'

    def get_buildtype_linker_args(self, buildtype: str) -> T.List[str]:
        return self._cook_link_args(self.host_compiler.get_buildtype_linker_args(buildtype))

    def build_rpath_args(self, env: 'Environment', build_dir: str, from_dir: str,
                         rpath_paths: str, build_rpath: str,
                         install_rpath: str) -> T.Tuple[T.List[str], T.Set[bytes]]:
        (rpath_args, rpath_dirs_to_remove) = self.host_compiler.build_rpath_args(
            env, build_dir, from_dir, rpath_paths, build_rpath, install_rpath)
        return (self._cook_link_args(rpath_args), rpath_dirs_to_remove)

    def linker_to_compiler_args(self, args: T.List[str]) -> T.List[str]:
        return args

    def get_pic_args(self) -> T.List[str]:
        return self._to_host_flags(self.host_compiler.get_pic_args())

    def compute_parameters_with_absolute_paths(self, parameter_list: T.List[str],
                                               build_dir: str) -> T.List[str]:
        return []

    def get_output_args(self, target: str) -> T.List[str]:
        return ['-o', target]

    def get_std_exe_link_args(self) -> T.List[str]:
        return self._cook_link_args(self.host_compiler.get_std_exe_link_args())

    def find_library(self, libname: str, env: 'Environment', extra_dirs: T.List[str],
                     libtype: LibType = LibType.PREFER_SHARED) -> T.Optional[T.List[str]]:
        return ['-l' + libname] # FIXME

    def get_crt_compile_args(self, crt_val: str, buildtype: str) -> T.List[str]:
        return self._to_host_flags(self.host_compiler.get_crt_compile_args(crt_val, buildtype))

    def get_crt_link_args(self, crt_val: str, buildtype: str) -> T.List[str]:
        # nvcc defaults to static, release version of msvc runtime and provides no
        # native option to override it; override it with /NODEFAULTLIB
        host_link_arg_overrides = []
        host_crt_compile_args = self.host_compiler.get_crt_compile_args(crt_val, buildtype)
        if any(arg in ['/MDd', '/MD', '/MTd'] for arg in host_crt_compile_args):
            host_link_arg_overrides += ['/NODEFAULTLIB:LIBCMT.lib']
        return self._cook_link_args(host_link_arg_overrides + self.host_compiler.get_crt_link_args(crt_val, buildtype))

    def get_target_link_args(self, target: 'BuildTarget') -> T.List[str]:
        return self._cook_link_args(super().get_target_link_args(target))

    def get_dependency_compile_args(self, dep: 'Dependency') -> T.List[str]:
        return self._to_host_flags(super().get_dependency_compile_args(dep))

    def get_dependency_link_args(self, dep: 'Dependency') -> T.List[str]:
        return self._cook_link_args(super().get_dependency_link_args(dep))
