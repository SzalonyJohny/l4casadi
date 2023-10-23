import os
import pathlib
import platform
from importlib.resources import files
from typing import Union, Optional, Callable, Text, Tuple

import casadi as cs
import torch
from torch.func import vmap, jacrev, hessian
from l4casadi.ts_compiler import ts_compile
from torch.fx.experimental.proxy_tensor import make_fx

from l4casadi.template_generation import render_casadi_c_template


def dynamic_lib_file_ending():
    return '.dylib' if platform.system() == 'Darwin' else '.so'


class L4CasADi(object):
    def __init__(self,
                 model: Callable[[torch.Tensor], torch.Tensor],
                 model_expects_batch_dim: bool = True,
                 device: Union[torch.device, Text] = 'cpu',
                 name: Text = 'l4casadi_f',
                 build_dir: Text = './_l4c_generated',
                 model_search_path: Optional[Text] = None):
        """
        :param model: PyTorch model.
        :param model_expects_batch_dim: True if the PyTorch model expects a batch dimension. This is commonly True
            for trained PyTorch models.
        :param device: Device on which the PyTorch model is executed.
        :param name: Unique name of the generated L4CasADi model. This name is used for autogenerated files.
            Creating two L4CasADi models with the same name will result in overwriting the files of the first model.
        :param model_search_path: Path to the directory where the PyTorch model can be found. By default, this will be
            the absolute path to the `build_dir` where the model traces are exported to. This parameter can become
            useful if the created L4CasADi dynamic library and the exported PyTorch Models are expected to be moved to a
            different folder (or another device).
        """
        self.model = model
        if isinstance(self.model, torch.nn.Module):
            self.model.eval().to(device)
            for parameters in self.model.parameters():
                parameters.requires_grad = False
        self.name = name
        self.has_batch = model_expects_batch_dim
        self.device = device if isinstance(device, str) else f'{device.type}:{device.index}'

        self.build_dir = pathlib.Path(build_dir)

        self._model_search_path = model_search_path

        self._cs_fun: Optional[cs.Function] = None
        self._built = False

    def __call__(self, *args):
        return self.forward(*args)

    @property
    def shared_lib_dir(self):
        return self.build_dir.absolute().as_posix()

    def forward(self, inp: Union[cs.MX, cs.SX, cs.DM]):
        if self.has_batch:
            if not inp.shape[-1] == 1:   # type: ignore[attr-defined]
                raise ValueError("For batched PyTorch models only vector inputs are allowed.")

        if not self._built:
            self.build(inp)
        if self._cs_fun is None:
            self._load_built_library_as_external_cs_fun()

        out = self._cs_fun(inp)  # type: ignore[misc]

        return out

    def maybe_make_generation_dir(self):
        if not os.path.exists(self.build_dir):
            os.makedirs(self.build_dir)

    def build(self, inp: Union[cs.MX, cs.SX, cs.DM]) -> None:
        """Builds the L4CasADi model as dynamic library.

        1. Exports the traced PyTorch model to TorchScript.
        2. Fills the C++ template with model parameters and paths to TorchScript.
        3. Compiles the C++ template to a dynamic library.
        4. Loads the dynamic library as CasADi external function.

        :param inp: Symbolic model input. Used to infer expected input shapes.
        """
        rows, cols = inp.shape  # type: ignore[attr-defined]

        self.maybe_make_generation_dir()
        has_jac, has_hess = self.export_torch_traces(rows, cols)

        if not has_jac:
            print('Jacobian trace could not be generated. First-order sensitivities will not be available in CasADi.')
        if not has_hess:
            print('Hessian trace could not be generated. Second-order sensitivities will not be available in CasADi.')

        self.generate_cpp_function_template(rows, cols, has_jac, has_hess)
        self.compile_cs_function()

        self._built = True

    def _load_built_library_as_external_cs_fun(self):
        if not self._built:
            raise RuntimeError('L4CasADi model has not been built yet. Call `build` first.')
        self._cs_fun = cs.external(
            f'{self.name}',
            f"{self.build_dir / f'lib{self.name}'}{dynamic_lib_file_ending()}"
        )

    def generate_cpp_function_template(self, rows: int, cols: int, has_jac: bool, has_hess: bool):
        if self.has_batch:
            rows_out = self.model(torch.zeros(1, rows).to(self.device)).shape[-1]
            cols_out = 1
        else:
            out_shape = self.model(torch.zeros(rows, cols).to(self.device)).shape
            if len(out_shape) == 1:
                rows_out = out_shape[0]
                cols_out = 1
            else:
                rows_out, cols_out = out_shape[-2:]

        model_path = (self.build_dir.absolute().as_posix()
                      if self._model_search_path is None
                      else self._model_search_path)

        gen_params = {
            'model_path': model_path,
            'device': self.device,
            'name': self.name,
            'rows_in': rows,
            'cols_in': cols,
            'rows_out': rows_out,
            'cols_out': cols_out,
            'has_jac': 'true' if has_jac else 'false',
            'has_hess': 'true' if has_hess else 'false',
            'model_expects_batch_dim': 'true' if self.has_batch else 'false',
        }

        render_casadi_c_template(
            variables=gen_params,
            out_file=(self.build_dir / f'{self.name}.cpp').as_posix()
        )

    def compile_cs_function(self):
        file_dir = files('l4casadi')
        include_dir = files('l4casadi') / 'include'
        lib_dir = file_dir / 'lib'

        # call gcc
        soname = 'install_name' if platform.system() == 'Darwin' else 'soname'
        cxx11_abi = 1 if torch._C._GLIBCXX_USE_CXX11_ABI else 0
        os_cmd = ("gcc"
                  " -fPIC -shared"
                  f" {self.build_dir / self.name}.cpp"
                  f" -o {self.build_dir / f'lib{self.name}'}{dynamic_lib_file_ending()}"
                  f" -I{include_dir} -L{lib_dir}"
                  f" -Wl,-{soname},lib{self.name}{dynamic_lib_file_ending()}"
                  " -ll4casadi -lstdc++ -std=c++17"
                  f" -D_GLIBCXX_USE_CXX11_ABI={cxx11_abi}")

        status = os.system(os_cmd)
        if status != 0:
            raise Exception(f'Compilation failed!\n\nAttempted to execute OS command:\n{os_cmd}\n\n')

    def _trace_jac_model(self, inp):
        if self.has_batch:
            return make_fx(vmap(jacrev(self.model)))(inp)
        else:
            return make_fx(jacrev(self.model))(inp)

    def _trace_hess_model(self, inp):
        if self.has_batch:
            return make_fx(vmap(hessian(self.model)))(inp)
        else:
            return make_fx(hessian(self.model))(inp)

    def export_torch_traces(self, rows: int, cols: int) -> Tuple[bool, bool]:
        if self.has_batch:
            d_inp = torch.zeros((1, rows))
        else:
            d_inp = torch.zeros((rows, cols))
        d_inp = d_inp.to(self.device)

        out_folder = self.build_dir

        torch.jit.trace(self.model, d_inp).save((out_folder / f'{self.name}_forward.pt').as_posix())

        jac_model = self._trace_jac_model(d_inp)

        hess_model = None
        try:
            hess_model = self._trace_hess_model(d_inp)
        except:  # noqa
            pass

        exported_jacrev = self._jit_compile_and_save(
            jac_model,
            (out_folder / f'{self.name}_jacrev.pt').as_posix(),
            d_inp
        )
        if hess_model is not None:
            exported_hess = self._jit_compile_and_save(
                hess_model,
                (out_folder / f'{self.name}_hess.pt').as_posix(),
                d_inp
            )
        else:
            exported_hess = False

        return exported_jacrev, exported_hess

    @staticmethod
    def _jit_compile_and_save(model, file_path: str, dummy_inp: torch.Tensor):
        # TODO: Could switch to torch export https://pytorch.org/docs/stable/export.html
        # Try tracing
        try:
            torch.jit.trace(model, dummy_inp).save(file_path)
        except:  # noqa
            # Try scripting
            try:
                ts_compile(model).save(file_path)
            except:  # noqa
                return False
        return True
