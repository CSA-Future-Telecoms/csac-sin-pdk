from __future__ import annotations

import hashlib
import itertools
import pathlib
from collections.abc import Sequence
from typing import Any, Literal

import numpy as np
import pydantic.v1 as pydantic
import tidy3d_tools as td
import xarray
from gdsfactory import logger
from gdsfactory.config import PATH
from gdsfactory.typings import PathType
from pydantic.v1 import BaseModel
from tidy3d.plugins import waveguide
from tqdm.auto import tqdm

from gplugins.tidy3d.materials import MaterialSpecTidy3d, get_medium
from gplugins.typings import NDArrayF


import gplugins.tidy3d as gtidy

class Waveguide(BaseModel, extra="forbid"):
    """Waveguide Model.

    All dimensions must be specified in μm (1e-6 m).

    Parameters:
        wavelength: wavelength in free space.
        core_width: waveguide core width.
        core_thickness: waveguide core thickness (height).
        core_material: core material. One of:
            - string: material name.
            - float: refractive index.
            - float, float: refractive index real and imaginary part.
            - td.Medium: tidy3d medium.
            - function: function of wavelength.
        clad_material: top cladding material.
        box_material: bottom cladding material.
        slab_thickness: thickness of the slab region in a rib waveguide.
        clad_thickness: thickness of the top cladding.
        box_thickness: thickness of the bottom cladding.
        side_margin: domain extension to the side of the waveguide core.
        sidewall_angle: angle of the core sidewall w.r.t. the substrate
            normal.
        sidewall_thickness: thickness of a layer on the sides of the
            waveguide core to model side-surface losses.
        sidewall_k: absorption coefficient added to the core material
            index on the side-surface layer.
        surface_thickness: thickness of a layer on the top of the
            waveguide core and slabs to model top-surface losses.
        surface_k: absorption coefficient added to the core material
            index on the top-surface layer.
        bend_radius: radius to simulate circular bend.
        num_modes: number of modes to compute.
        group_index_step: if set to `True`, indicates that the group
            index must also be calculated. If set to a positive float
            it defines the fractional frequency step used for the
            numerical differentiation of the effective index.
        precision: computation precision.
        grid_resolution: wavelength resolution of the computation grid.
        max_grid_scaling: grid scaling factor in cladding regions.
        cache_path: Optional path to the cache directory. None disables cache.
        overwrite: overwrite cache.

    ::

        ________________________________________________
                                                ^
                                                ¦
                                                ¦
                                          clad_thickness
                       |<--core_width-->|       ¦
                                                ¦
                       .________________.      _v_
                       |       ^        |
        <-side_margin->|       ¦        |
                       |       ¦        |
        _______________'       ¦        '_______________
              ^          core_thickness
              ¦                ¦
        slab_thickness         ¦
              ¦                ¦
              v                v
        ________________________________________________
                               ^
                               ¦
                         box_thickness
                               ¦
                               v
        ________________________________________________
    """

    wavelength: float | Sequence[float] | Any
    core_width: float
    core_thickness: float
    core_material: MaterialSpecTidy3d
    clad_material: MaterialSpecTidy3d
    box_material: MaterialSpecTidy3d | None = None
    slab_thickness: float = 0.0
    clad_thickness: float | None = None
    box_thickness: float | None = None
    side_margin: float | None = None
    sidewall_angle: float = 0.0
    sidewall_thickness: float = 0.0
    sidewall_k: float = 0.0
    surface_thickness: float = 0.0
    surface_k: float = 0.0
    bend_radius: float | None = None
    num_modes: int = 2
    group_index_step: bool | float = False
    precision: Precision = "double"
    grid_resolution: int = 20
    max_grid_scaling: float = 1.2
    cache_path: PathType | None = PATH.modes
    overwrite: bool = False

    _cached_data = pydantic.PrivateAttr()
    _waveguide = pydantic.PrivateAttr()

    @pydantic.validator("wavelength")
    def _fix_wavelength_type(cls, v: Any) -> NDArrayF:
        return np.array(v, dtype=float)

    @property
    def filepath(self) -> pathlib.Path | None:
        """Cache file path."""
        if not self.cache_path:
            return None
        cache_path = pathlib.Path(self.cache_path)
        cache_path.mkdir(exist_ok=True, parents=True)

        settings = [
            f"{setting}={custom_serializer(getattr(self, setting))}"
            for setting in sorted(self.__fields__.keys())
        ]
        named_args_string = "_".join(settings)
        h = hashlib.md5(named_args_string.encode()).hexdigest()[:16]
        return cache_path / f"{self.__class__.__name__}_{h}.npz"

    @property
    def waveguide(self):
        """Tidy3D waveguide used by this instance."""
        # if (not hasattr(self, "_waveguide")
        #         or isinstance(self.core_material, td.CustomMedium)):
        if not hasattr(self, "_waveguide"):
            # To include a dn -> custom medium
            if isinstance(self.core_material, td.CustomMedium | td.Medium):
                core_medium = self.core_material
            else:
                core_medium = get_medium(self.core_material)

            if isinstance(self.clad_material, td.CustomMedium | td.Medium):
                clad_medium = self.clad_material
            else:
                clad_medium = get_medium(self.clad_material)

            if self.box_material:
                if isinstance(self.box_material, td.CustomMedium | td.Medium):
                    box_medium = self.box_material
                else:
                    box_medium = get_medium(self.box_material)
            else:
                box_medium = None

            freq0 = td.C_0 / np.mean(self.wavelength)
            n_core = core_medium.eps_model(freq0) ** 0.5
            n_clad = clad_medium.eps_model(freq0) ** 0.5

            sidewall_medium = (
                td.Medium.from_nk(
                    n=n_clad.real, k=n_clad.imag + self.sidewall_k, freq=freq0
                )
                if self.sidewall_k != 0.0
                else None
            )
            surface_medium = (
                td.Medium.from_nk(
                    n=n_clad.real, k=n_clad.imag + self.surface_k, freq=freq0
                )
                if self.surface_k != 0.0
                else None
            )

            mode_spec = td.ModeSpec(
                num_modes=self.num_modes,
                target_neff=n_core.real,
                bend_radius=self.bend_radius,
                bend_axis=1,
                num_pml=(12, 12) if self.bend_radius else (0, 0),
                precision=self.precision,
                group_index_step=self.group_index_step,
            )

            self._waveguide = waveguide.RectangularDielectric(
                wavelength=self.wavelength,
                core_width=self.core_width,
                core_thickness=self.core_thickness,
                core_medium=core_medium,
                clad_medium=clad_medium,
                box_medium=box_medium,
                slab_thickness=self.slab_thickness,
                clad_thickness=self.clad_thickness,
                box_thickness=self.box_thickness,
                side_margin=self.side_margin,
                sidewall_angle=self.sidewall_angle,
                sidewall_thickness=self.sidewall_thickness,
                sidewall_medium=sidewall_medium,
                surface_thickness=self.surface_thickness,
                surface_medium=surface_medium,
                propagation_axis=2,
                normal_axis=1,
                mode_spec=mode_spec,
                grid_resolution=self.grid_resolution,
                max_grid_scaling=self.max_grid_scaling,
            )

        return self._waveguide

    @property
    def _data(self):
        """Mode data for this waveguide (cached if cache is enabled)."""
        if not hasattr(self, "_cached_data"):
            filepath = self.filepath
            if filepath and filepath.exists() and not self.overwrite:
                logger.info(f"load data from {filepath}.")
                self._cached_data = np.load(filepath)
                return self._cached_data

            wg = self.waveguide

            fields = wg.mode_solver.data.field_components
            self._cached_data = {
                f + c: fields[f + c].squeeze(drop=True).values
                for f in "EH"
                for c in "xyz"
            }

            self._cached_data["x"] = fields["Ex"].coords["x"].values
            self._cached_data["y"] = fields["Ex"].coords["y"].values

            self._cached_data["n_eff"] = wg.n_complex.squeeze(drop=True).values
            self._cached_data["mode_area"] = wg.mode_area.squeeze(drop=True).values

            fraction_te = np.zeros(self.num_modes)
            fraction_tm = np.zeros(self.num_modes)

            for i in range(self.num_modes):
                e_fields = (
                    fields["Ex"].sel(mode_index=i),
                    fields["Ey"].sel(mode_index=i),
                )
                areas_e = [np.sum(np.abs(e) ** 2) for e in e_fields]
                areas_e /= np.sum(areas_e)
                areas_e *= 100
                fraction_te[i] = areas_e[0] / (areas_e[0] + areas_e[1])
                fraction_tm[i] = areas_e[1] / (areas_e[0] + areas_e[1])

            self._cached_data["fraction_te"] = fraction_te
            self._cached_data["fraction_tm"] = fraction_tm

            if wg.n_group is not None:
                self._cached_data["n_group"] = wg.n_group.squeeze(drop=True).values

            if filepath:
                logger.info(f"store data into {filepath}.")
                np.savez(filepath, **self._cached_data)

        return self._cached_data

    @property
    def fraction_te(self):
        """Fraction of TE polarization."""
        return self._data["fraction_te"]

    @property
    def fraction_tm(self):
        """Fraction of TM polarization."""
        return self._data["fraction_tm"]

    @property
    def n_eff(self):
        """Effective propagation index."""
        return self._data["n_eff"]

    @property
    def n_group(self):
        """Group index.

        This is only present it the parameter `group_index_step` is set.
        """
        return self._data.get("n_group", None)

    @property
    def mode_area(self):
        """Effective mode area."""
        return self._data["mode_area"]

    @property
    def loss_dB_per_cm(self):
        """Propagation loss for computed modes in dB/cm."""
        wavelength = self.wavelength * 1e-6  # convert to m
        alpha = 2 * np.pi * np.imag(self.n_eff).T / wavelength  # lin/m loss
        return 20 * np.log10(np.e) * alpha.T * 1e-2  # dB/cm loss

    @property
    def index(self) -> None:
        """Refractive index distribution on the simulation domain."""
        plane = self.waveguide.mode_solver.plane
        wavelength = (
            self.wavelength[self.wavelength.size // 2]
            if self.wavelength.size > 1
            else self.wavelength
        )
        eps = self.waveguide.mode_solver.simulation.epsilon(
            plane, freq=td.C_0 / wavelength
        )
        return eps.squeeze(drop=True).T ** 0.5

    def overlap(self, waveguide: Waveguide, conjugate: bool = True):
        """Calculate the mode overlap between waveguide modes.

        Parameters:
            waveguide: waveguide with which to overlap modes.
            conjugate: use the conjugate form of the overlap integral.
        """
        self_data = self.waveguide.mode_solver.data
        other_data = waveguide.waveguide.mode_solver.data
        # self_data = self._data
        # other_data = waveguide._data
        return self_data.outer_dot(other_data, conjugate).squeeze(drop=True).values

    def plot_grid(self) -> None:
        """Plot the waveguide grid."""
        self.waveguide.plot_grid(z=0)

    def plot_index(self, **kwargs):
        """Plot the waveguide index distribution.

        Keyword arguments are passed to xarray.DataArray.plot.
        """
        artist = self.index.real.plot(**kwargs)
        artist.axes.set_aspect("equal")
        return artist

    def plot_field(
        self,
        field_name: str,
        value: str = "real",
        mode_index: int = 0,
        wavelength: float | None = None,
        **kwargs,
    ):
        """Plot the selected field distribution from a waveguide mode.

        Parameters:
            field_name: one of 'Ex', 'Ey', 'Ez', 'Hx', 'Hy', 'Hz'.
            value: component of the field to plot. One of 'real',
                'imag', 'abs', 'phase', 'dB'.
            mode_index: mode selection.
            wavelength: wavelength selection.
            kwargs: keyword arguments passed to xarray.DataArray.plot.
        """
        data = self._data[field_name]

        if mode_index >= self.num_modes:
            raise ValueError(
                f"mode_index = {mode_index} must be less than num_modes {self.num_modes}"
            )

        if self.num_modes > 1:
            data = data[..., mode_index]
        if self.wavelength.size > 1:
            i = (
                np.argmin(np.abs(wavelength - self.wavelength))
                if wavelength
                else self.wavelength.size // 2
            )
            data = data[..., i]

        if value == "real":
            data = data.real
        elif value == "imag":
            data = data.imag
        elif value == "abs":
            data = np.abs(data)
        elif value == "dB":
            data = 20 * np.log10(np.abs(data))
            data -= np.max(data)
        elif value == "phase":
            data = np.arctan2(data.imag, data.real)
        else:
            raise ValueError(
                "value must be one of 'real', 'imag', 'abs', 'phase', 'dB'"
            )
        data_array = xarray.DataArray(
            data.T, coords={"y": self._data["y"], "x": self._data["x"]}
        )

        if value == "dB":
            kwargs.update(vmin=-20)

        data_array.name = field_name
        artist = data_array.plot(**kwargs)
        artist.axes.set_aspect("equal")
        return artist

    def _ipython_display_(self) -> None:
        """Show index in matplotlib for Jupyter Notebooks."""
        self.plot_index()

    def __repr__(self) -> str:
        """Show waveguide representation."""
        return (
            f"{self.__class__.__name__}("
            + ", ".join(
                f"{k}={custom_serializer(getattr(self, k))!r}"
                for k in self.__fields__.keys()
            )
            + ")"
        )

    def __str__(self) -> str:
        """Show waveguide representation."""
        return self.__repr__()