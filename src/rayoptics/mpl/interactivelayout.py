#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright © 2019 Michael J. Hayford
""" Interactive layout figure with paraxial editing

.. Created on Thu Mar 14 10:20:33 2019

.. codeauthor: Michael J. Hayford
"""

import numpy as np

from rayoptics.gui.util import bbox_from_poly

from rayoptics.mpl.interactivefigure import InteractiveFigure

from rayoptics.gui.layout import LensLayout
from rayoptics.util.rgb2mpl import rgb2mpl, backgrnd_color


class InteractiveLayout(InteractiveFigure):
    """ Editable version of optical system layout, aka Live Layout

    Attributes:
        opt_model: parent optical model
        refresh_gui: function to be called on refresh_gui event
        offset_factor: how much to draw rays before first surface
        do_draw_rays: if True, draw edge rays
        do_paraxial_layout: if True, draw editable paraxial axial and chief ray
    """
    def __init__(self, opt_model, refresh_gui,
                 offset_factor=0.05,
                 do_draw_rays=False,
                 do_paraxial_layout=True,
                 **kwargs):
        self.refresh_gui = refresh_gui
        self.layout = LensLayout(opt_model)
        self.linewidth = 0.5
        self.do_draw_rays = do_draw_rays
        self.do_paraxial_layout = do_paraxial_layout
        self.offset_factor = offset_factor
        self.do_scale_bounds = True

        super().__init__(**kwargs)

        self.update_data()

    def update_data(self):
        self.artists = []
        concat_bbox = []
        layout = self.layout

        self.ele_shapes = layout.create_element_model(self)
        self.ele_bbox = self.update_patches(self.ele_shapes)
        concat_bbox.append(self.ele_bbox)

        if self.do_draw_rays:
            sl_so = layout.system_length(self.ele_bbox,
                                         offset_factor=self.offset_factor)
            system_length, start_offset = sl_so
            self.ray_shapes = layout.create_ray_model(self, start_offset)
            self.ray_bbox = self.update_patches(self.ray_shapes)
            concat_bbox.append(self.ray_bbox)

        if self.do_paraxial_layout:
            self.parax_shapes = layout.create_paraxial_layout(self)
            self.parax_bbox = self.update_patches(self.parax_shapes)
            concat_bbox.append(self.parax_bbox)

        sys_bbox = np.concatenate(concat_bbox)
        self.sys_bbox = bbox_from_poly(sys_bbox)

        return self
