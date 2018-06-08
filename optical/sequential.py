#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright © 2018 Michael J. Hayford
""" Manager class for a sequential optical model

@author: Michael J. Hayford
"""

import itertools
import logging
from . import opticalspec
from . import surface
from . import profiles
from . import gap
from . import medium as m
from . import raytrace as rt
from . import transform as trns
from optical.model_constants import Surf, Gap
from optical.model_constants import ax, pr, lns
from optical.model_constants import ht, slp, aoi
from optical.model_constants import pwr, tau, indx, rmd
from glass import glassfactory as gfact
from glass import glasserror as ge
import numpy as np
import transforms3d as t3d
from math import sqrt, copysign
from util.misc_math import euler2opt, isanumber


class SequentialModel:
    """ Manager class for a sequential optical model

    A sequential optical model is a sequence of surfaces and gaps. It includes
    optical usage information to specify the aperture, field of view and
    spectrum.

    The sequential model has this structure:
        SObj   S1    S2    S3 ... Si-1      SImg
           \  /  \  /  \  /           \    /
           GObj   G1    G2             Gi-1
    where:
        S is a Surface instance
        G is a Gap instance

    There are N surfaces and N-1 gaps. The initial configuration has an object
    and image surface and an object gap.

    The Surface class maintains the profile and extent of the interface. The
    Gap class maintains a simple separation (z translation) and the medium
    filling the gap. More complex coordinate transformations are handled
    through the Surface.
    """

    def __init__(self, opt_model):
        self.parent = opt_model
        self.surfs = []
        self.gaps = []
        self.transforms = []
        self.stop_surface = 1
        self.cur_surface = 0
        self.optical_spec = opticalspec.OpticalSpecs()
        self.surfs.append(surface.Surface('Obj'))
        self.surfs.append(surface.Surface('Img'))
        self.gaps.append(gap.Gap())

    def __json_encode__(self):
        attrs = dict(self.__dict__)
        del attrs['parent']
        del attrs['transforms']
        return attrs

    def reset(self):
        self.__init__()

    def get_num_surfaces(self):
        return len(self.surfs)

    def get_surface_and_gap(self, srf=None):
        if not srf:
            srf = self.cur_surface
        s = self.surfs[srf]
        if srf == len(self.gaps):
            g = None
        else:
            g = self.gaps[srf]
        return s, g

    def set_cur_surface(self, s):
        self.cur_surface = s

    def __iadd__(self, node):
        if isinstance(node, gap.Gap):
            self.gaps.append(node)
        else:
            self.surfs.insert(len(self.surfs)-1, node)
        return self

    def insert(self, surf, gap):
        """ insert surf and gap at the cur_gap edge of the sequential model
            graph """
        self.cur_surface += 1
        self.surfs.insert(self.cur_surface, surf)
        self.gaps.insert(self.cur_surface, gap)

    def add_surface(self, surf):
        """ add a surface where surf is a list that contains:
            [curvature, thickness, refractive_index, v-number] """
        s, g = self.create_surface_and_gap(surf)
        self.insert(s, g)

    def update_surface(self, surf):
        """ add a surface where surf is a list that contains:
            [curvature, thickness, refractive_index, v-number] """

        self.insert(*self.create_surface_and_gap(surf))

    def sync_to_restore(self, opt_model):
        self.parent = opt_model
        for sg in itertools.zip_longest(self.surfs, self.gaps):
            if hasattr(sg[Surf], 'sync_to_restore'):
                sg[Surf].sync_to_restore(self)
            if sg[Gap]:
                if hasattr(sg[Gap], 'sync_to_restore'):
                    sg[Gap].sync_to_restore(self)

    def update_model(self):
        # delta n across each surface interface must be set to some
        #  reasonable default value. use the index at the central wavelength
        wl = self.optical_spec.spectral_region.central_wvl()
        n_before = self.gaps[0].medium.rindex(wl)

        for sg in itertools.zip_longest(self.surfs, self.gaps):
            if sg[Gap]:
                n_after = copysign(sg[Gap].medium.rindex(wl), n_before)
                if sg[Surf].refract_mode == 'REFL':
                    n_after = -n_after
                sg[Surf].delta_n = n_after - n_before
                n_before = n_after
            else:
                sg[Surf].delta_n = 0.0
            # call update() on the surface interface
            sg[Surf].update()

        self.transforms = self.compute_global_coords()
        self.optical_spec.update_model(self)
        self.set_clear_apertures()

    def insert_surface_and_gap(self):
        s = surface.Surface()
        g = gap.Gap()
        self.insert(s, g)
        return s, g

    def update_surface_and_gap_from_cv_input(self, dlist, idx=None):
        if isinstance(idx, int):
            s, g = self.get_surface_and_gap(idx)
        else:
            s, g = self.insert_surface_and_gap()

        if self.parent.radius_mode:
            if dlist[0] != 0.0:
                s.profile.cv = 1.0/dlist[0]
            else:
                s.profile.cv = 0.0
        else:
            s.profile.cv = dlist[0]

        if g:
            g.thi = dlist[1]
            if len(dlist) < 3:
                g.medium = m.Air()
            elif dlist[2].upper() == 'REFL':
                s.refract_mode = 'REFL'
                g.medium = self.gaps[self.cur_surface-1].medium
            elif isanumber(dlist[2]):
                n, v = m.glass_decode(float(dlist[2]))
                g.medium = m.Glass(n, v, '')
            else:
                name, cat = dlist[2].split('_')
                if cat.upper() == 'SCHOTT' and name[:1].upper() == 'N':
                    name = name[:1]+'-'+name[1:]
                try:
                    g.medium = gfact.create_glass(cat, name)
                except ge.GlassNotFoundError as gerr:
                    logging.info('%s glass data type %s not found',
                                 gerr.catalog,
                                 gerr.name)
                    logging.info('Replacing material with air.')
                    g.medium = m.Air()
        else:
            # at image surface, apply defocus to previous thickness
            self.gaps[idx-1].thi += dlist[1]

    def update_surface_profile_cv_input(self, profile_type, idx=None):
        if not isinstance(idx, int):
            idx = self.cur_surface
        cur_profile = self.surfs[idx].profile
        new_profile = profiles.mutate_profile(cur_profile, profile_type)
        self.surfs[idx].profile = new_profile
        return self.surfs[idx].profile

    def update_surface_decenter_cv_input(self, idx=None):
        if not isinstance(idx, int):
            idx = self.cur_surface
        if not self.surfs[idx].decenter:
            self.surfs[idx].decenter = surface.DecenterData()
        return self.surfs[idx].decenter

    def create_surface_and_gap(self, surf):
        """ create a surface and gap where surf is a list that contains:
            [curvature, thickness, refractive_index, v-number] """
        s = surface.Surface()
        s.profile.cv = surf[0]

        if surf[3] == 0.0:
            if surf[2] == 1.0:
                mat = m.Air()
            else:
                mat = m.Medium(surf[2])
        else:
            mat = m.Glass(surf[2], surf[3], '')

        g = gap.Gap(surf[1], mat)
        return s, g

    def surface_label_list(self):
        """ list of surface labels or surface number, if no label """
        labels = []
        for i, s in enumerate(self.surfs):
            if len(s.label) == 0:
                if i == self.stop_surface:
                    labels.append('Stop')
                else:
                    labels.append(str(i))
            else:
                labels.append(s.label)
        return labels

    def seq_model_to_paraxial_lens(self):
        """ returns lists of power, reduced thickness, signed index and refract mode
        """
        wl = self.optical_spec.spectral_region.central_wvl()
        n_before = self.gaps[0].medium.rindex(wl)
        pwr = []
        tau = []
        indx = []
        rmd = []
        for sg in itertools.zip_longest(self.surfs, self.gaps):
            if sg[Gap]:
                n_after = copysign(sg[Gap].medium.rindex(wl), n_before)
                rmode = sg[Surf].refract_mode
                if rmode == 'REFL':
                    n_after = -n_after

                tau.append(sg[Gap].thi/n_after)
                indx.append(n_after)
                rmd.append(rmode)

                power = sg[Surf].optical_power
                pwr.append(power)

                n_before = n_after
            else:
                tau.append(0.0)
                indx.append(n_before)
                rmd.append(rmode)
                pwr.append(0.0)
        return [pwr, tau, indx, rmd]

    def paraxial_lens_to_seq_model(self, lens):
        """ Applies a paraxial lens spec (power, reduced distance) to the model data

        lens: list of paraxia axial, chief and lens data
        """
        power = lens[lns][pwr]
        rindx = lens[lns][indx]
        n_before = rindx[0]
        slp_before = lens[ax][slp][0]
        for i, sg in enumerate(itertools.zip_longest(self.surfs, self.gaps)):
            if sg[Gap]:
                n_after = rindx[i]
                slp_after = lens[ax][slp][i]
                sg[Gap].thi = n_after*lens[lns][tau][i]

                sg[Surf].set_optical_power(power[i], n_before, n_after)
                sg[Surf].from_first_order(slp_before, slp_after,
                                          lens[ax][ht][i])

                n_before = n_after
                slp_before = slp_after

    def list_model(self):
        for i, sg in enumerate(itertools.zip_longest(self.surfs, self.gaps)):
            if sg[Gap]:
                print(i, sg[Surf])
                print('    ', sg[Gap])
            else:
                print(i, sg[Surf])

    def list_gaps(self):
        for i, gp in enumerate(self.gaps):
            print(i, gp)

    def list_surfaces(self):
        for i, s in enumerate(self.surfs):
            print(i, s)

    def list_surface_and_gap(self, i):
        s = self.surfs[i]
        cvr = s.profile.cv
        if self.parent.radius_mode:
            if cvr != 0.0:
                cvr = 1.0/cvr
        sd = s.surface_od()

        if i < len(self.gaps):
            g = self.gaps[i]
            thi = g.thi
            med = g.medium.name()
        else:
            thi = ''
            med = ''
        return [cvr, thi, med, sd]

    def list_decenters(self):
        for i, sg in enumerate(itertools.zip_longest(self.surfs, self.gaps)):
            if sg[Gap]:
                print(i, sg[Gap])
                if sg[Surf].decenter is not None:
                    print(' ', repr(sg[Surf].decenter))
            else:
                if sg[Surf].decenter is not None:
                    print(i, repr(sg[Surf].decenter))

    def list_elements(self):
        for i, gp in enumerate(self.gaps):
            if gp.medium.label.lower() != 'air':
                print(self.surfs[i].profile,
                      self.surfs[i+1].profile,
                      gp)

    def trace_boundary_rays_at_field(self, fi):
        pupil_rays = [[0., 0.], [1., 0.], [-1., 0.], [0., 1.], [0., -1.]]
        rim_rays = []
        for r in pupil_rays:
            ray, op = self.optical_spec.trace(self, r, fi)
            rim_rays.append([ray, op])
        return rim_rays

    def trace_boundary_rays(self):
        rayset = []
        fov = self.optical_spec.field_of_view
        for fi, f in enumerate(fov.fields):
            rim_rays = self.trace_boundary_rays_at_field(fi)
            rayset.append(rim_rays)
        return rayset

    def trace_fan(self, fi, xy, num_rays=21):
        """ xy determines whether x (=0) or y (=1) fan """
        chief_ray, _ = self.optical_spec.trace(self, [0., 0.], fi)
        central_coord = chief_ray[-1][0]
        wvls = self.optical_spec.spectral_region
        fans_x = []
        fans_y = []
        fan_start = np.array([0., 0.])
        fan_stop = np.array([0., 0.])
        fan_start[xy] = -1.0
        fan_stop[xy] = 1.0
        fan_def = [fan_start, fan_stop, num_rays]
        max_y_val = 0.0
        rc = []
        for wi, w in enumerate(wvls.wavelengths):
            rc.append(wvls.render_colors[wi])
            fan = self.optical_spec.trace_fan(self, fan_def, fi, True, wi)
            f_x = []
            f_y = []
            for p, r in fan:
                f_x.append(p[xy])
                y_val = r[xy] - central_coord[xy]
                f_y.append(y_val)
                if abs(y_val) > max_y_val:
                    max_y_val = abs(y_val)
            fans_x.append(f_x)
            fans_y.append(f_y)
        fans_x = np.array(fans_x)
        fans_y = np.array(fans_y)
        return fans_x, fans_y, max_y_val, rc

    def shift_start_of_ray_bundle(self, rayset, start_offset, r, t):
        """ modify rayset so that rays begin "start_offset" from 1st surface

        rayset: list of rays in a bundle, i.e. all for one field. rayset[0]
                is assumed to be the chief ray
        start_offset: z distance rays should start wrt first surface.
                      positive if to left of first surface
        r, t: transformation rotation and translation
        """
        for ri, ray in enumerate(rayset):
            b4_pt = r.dot(ray[0][1][0]) + t
            b4_dir = r.dot(ray[0][0][1])
            if ri == 0:
                # For the chief ray, use the input offset.
                dst = -start_offset
            else:
                pt0 = rayset[0][0][0][0]
                dir0 = rayset[0][0][0][1]
                # Calculate distance along ray to plane perpendicular to
                #  the chief ray.
                dst = -(b4_pt - pt0).dot(dir0)/b4_dir.dot(dir0)
            pt = b4_pt + dst*b4_dir
            ray[0][0][0] = pt
            ray[0][0][1] = b4_dir

    def setup_shift_of_ray_bundle(self, start_offset):
        """ compute transformation for rays "start_offset" from 1st surface

        start_offset: z distance rays should start wrt first surface.
                      positive if to left of first surface
        return: transformation rotation and translation
        """
        s1 = self.surfs[1]
        s0 = self.surfs[0]
        g0 = gap.Gap(start_offset, self.gaps[0].medium)
        r, t = trns.reverse_transform(s1, g0, s0)
        return r, t

    def shift_start_of_rayset(self, rayset, start_offset):
        """ modify rayset so that rays begin "start_offset" from 1st surface

        rayset: list of ray bundles, i.e. for a list of fields
        start_offset: z distance rays should start wrt first surface.
                      positive if to left of first surface
        return: transformation rotation and translation
        """
        r, t = self.setup_shift_of_ray_bundle(start_offset)
        for ray_bundle in rayset:
            self.shift_start_of_ray_bundle(ray_bundle, start_offset, r, t)
        return r, t

    def set_clear_apertures(self):
        rayset = self.trace_boundary_rays()
        for i, s in enumerate(self.surfs):
            max_ap = -1.0e+10
            for f in rayset:
                for p in f:
                    ap = sqrt(p[0][i][0][0]**2 + p[0][i][0][1]**2)
                    if ap > max_ap:
                        max_ap = ap
            s.set_max_aperture(max_ap)

    def trace(self, pt0, dir0, wl, eps=1.0e-12):
        path = itertools.zip_longest(self.surfs, self.gaps)
        return rt.trace(path, pt0, dir0, wl, eps)

    def compute_global_coords(self, glo=1):
        """ Return global surface coordinates (rot, t) wrt surface glo. """
        tfrms = []
        r, t = np.identity(3), np.array([0., 0., 0.])
        prev = r, t
        tfrms.append(prev)
#        print(glo, t, *np.rad2deg(t3d.euler.mat2euler(r)))
        if glo > 0:
            # iterate in reverse over the segments before the
            #  global reference surface
            go = glo
            path = itertools.zip_longest(self.surfs[glo::-1],
                                         self.gaps[glo-1::-1])
            after = next(path)
            # loop of remaining surfaces in path
            while True:
                try:
                    before = next(path)
                    go -= 1
                    r, t = trns.reverse_transform(before[Surf], after[Gap],
                                                  after[Surf])
                    t = prev[0].dot(t) + prev[1]
                    r = prev[0].dot(r)
#                    print(go, t,
#                          *np.rad2deg(euler2opt(t3d.euler.mat2euler(r))))
                    prev = r, t
                    tfrms.append(prev)
                    after = before
                except StopIteration:
                    break
            tfrms.reverse()
        path = itertools.zip_longest(self.surfs[glo:], self.gaps[glo:])
        before = next(path)
        prev = np.identity(3), np.array([0., 0., 0.])
        go = glo
        # loop forward over the remaining surfaces in path
        while True:
            try:
                after = next(path)
                go += 1
                r, t = trns.forward_transform(before[Surf], before[Gap],
                                              after[Surf])
                t = prev[0].dot(t) + prev[1]
                r = prev[0].dot(r)
#                print(go, t,
#                      *np.rad2deg(euler2opt(t3d.euler.mat2euler(r))))
                prev = r, t
                tfrms.append(prev)
                before = after
            except StopIteration:
                break
        return tfrms

    def compute_global_coords_wrt(self, rng, go=1):
        for s in rng:
            pass
