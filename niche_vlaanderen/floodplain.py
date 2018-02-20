from __future__  import division

import rasterio
import os
from pkg_resources import resource_filename
import copy

import pandas as pd
import numpy as np

from .spatial_context import SpatialContext
from .codetables import validate_tables_floodplains, check_codes_used


class FloodPlainException(Exception):
    """"""


class FloodPlain(object):
    """
    Predict the vegetation response to (frequent) flooding

    A Floodplain object can be used to predict the response of vegetation to
    (frequent) flooding.

    The code tables used can be overwritten when initializing the object.
    Optionally also a name can be given which is used in plots and output
    files.

    """
    def __init__(self, depths=None, duration=None, frequency=None,
                 lnk_potential=None, potential=None, name=None):
        self._ct = dict()
        self._veg = dict()

        for i in ["depths", "duration", "frequency", "lnk_potential",
                  "potential"]:
            if locals()[i] is None:
                ct = resource_filename(
                        "niche_vlaanderen",
                        "system_tables/floodplains/"+i+".csv")
            else:
                ct = locals()[i]
            self._ct[i] = pd.read_csv(ct)
        self.name = name

        validate_tables_floodplains(**self._ct)

    def _calculate(self, depth, frequency, duration, period):
        """
        Low level calculation of a floodplains object.
        Uses a numpy array for depth rather than a grid file (in calculate)
        """
        orig_shape = depth.shape
        depth = depth.flatten()
        nodata = (depth == -99)

        check_codes_used("depth", depth,
                         self._ct["depths"]["code"])
        check_codes_used("frequency", frequency,
                         self._ct["frequency"]["code"])
        check_codes_used("duration", duration,
                         self._ct["duration"]["code"])
        check_codes_used("period", period,
                         ["summer","winter"])

        for veg_code, subtable_veg in \
                self._ct["lnk_potential"].groupby(["veg_code"]):
            subtable_veg = subtable_veg.reset_index()
            # by default we give code 4 (no information/flooding)
            # https://github.com/inbo/niche_vlaanderen/issues/87
            self._veg[veg_code] = np.full(depth.shape, 4, dtype="int16")
            self._veg[veg_code][nodata] = -99
            groupby_cols = ["period", "frequency", "duration"]
            for index, subtable in subtable_veg.groupby(groupby_cols):
                if (period, frequency, duration) == index:
                    subtable.reset_index()
                    for row in subtable.itertuples():
                        veg = self._veg[veg_code]
                        veg[row.depth == depth] = row.potential
                        self._veg[veg_code] = np.maximum(veg,
                                                         self._veg[veg_code])
            self._veg[veg_code] = self._veg[veg_code].reshape(orig_shape)

    def calculate(self, depth_file, frequency, period, duration):
        """ Calculate a floodplain object

        Parameters
        ==========
        depth_file: filename
           The filename containing a classified grid with inundation dephts.
           The classes used must correspond to the ones in the depths.csv code
           table.

        frequency: code
           The frequency with which flooding occurs, eg T2, T50. Valid values
           are given in the frequency.csv code table.

        period: winter|summer
            period in which the flooding occurs. Must be either "summer" or
            "winter"

        duration: code
            Period with which the flooding occurs, from duration.csv
             * 1: <14 days
             * 2: >14 days

        """
        with rasterio.open(depth_file) as dst:
            depth = dst.read(1)
            self._context = SpatialContext(dst)
            depth[depth == dst.nodatavals[0]] = -99
        self._calculate(depth, frequency, duration, period)

        self.options = {'frequency': frequency,
                        "duration": duration,
                        "period": period}

    def plot(self, key, ax=None):
        try:
            import matplotlib.pyplot as plt
            import matplotlib.patches as mpatches
            from matplotlib.colors import Normalize

        except (ImportError, RuntimeError):  # pragma: no cover
            msg = "Could not import matplotlib\n"
            msg += "matplotlib required for plotting functions"
            raise ImportError(msg)

        if key not in self._veg.keys():
            msg = "vegetation type {} not modeled".format(key)
            raise FloodPlainException(msg)

        if ax is None:
            fig, ax = plt.subplots()

        ((a, b), (c, d)) = self._context.extent
        mpl_extent = (a, c, d, b)

        im = plt.imshow(self._veg[key], extent=mpl_extent,
                        norm=Normalize(0, 4))
        options = self.options.copy()
        options["duration"] = "< 14 days" \
            if self.options["duration"] == 1 else "> 14 days"
        ax.set_title("{} ({})".format(key, options))

        labels = self._ct["potential"]["description"]
        values = self._ct["potential"]["code"]

        colors = [im.cmap(i/(len(values) - 1)) for (i, value) in enumerate(values)]
        patches = [mpatches.Patch(color=colors[i],
                                  label=labels[i]) for (i, value) in enumerate(values)]
        plt.legend(handles=patches, bbox_to_anchor=(1.05, 1), loc=2,
                   borderaxespad=0.)

        return ax

    def write(self, folder):
        if len(self._veg) == 0:
            raise FloodPlainException(
                "A valid run must be done before writing the output.")

        if not os.path.exists(folder):
            os.makedirs(folder)

        params = dict(
            driver='GTiff',
            height=self._context.height,
            width=self._context.width,
            crs=self._context.crs,
            transform=self._context.transform,
            count=1,
            dtype="int16",
            nodata=-99,
            compress="DEFLATE"
        )

        self._files_written = dict()
        name = ""
        if self.name is not None:
            name = self.name + "-"

        for vi in self._veg:
            filename = "{}F{:02d}-{}-P{}-{}.tif".format(name,
                vi, self.options["frequency"], self.options["duration"],
                self.options["period"])
            path = folder + "/" + filename
            with rasterio.open(path, 'w', **params) as dst:
                dst.write(self._veg[vi], 1)
                self._files_written[filename] = os.path.normpath(path)

    def combine(self, niche_result):
        # check niche model has been run
        if not niche_result.vegetation_calculated:
            raise FloodPlainException(
                "Niche model must be run prior to running this module.")

        if len(self._veg) == 0:
            raise FloodPlainException(
                "Floodplain model must be run prior to running this module.")

        if self._context != niche_result._context:
            raise FloodPlainException(
                "Niche model has a different spatial context:\n" +
                str(self._context) + str(niche_result._context)
                )

        new = copy.copy(self)
        for vi in new._veg:
            new._veg[vi] = niche_result._vegetation[vi] * new._veg[vi]
            nodata = ((niche_result._vegetation[vi] == 255) |
                      (new._veg[vi] ==-99))
            new._veg[vi][nodata] = -99

        return (new)
