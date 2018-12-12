#!/usr/bin/env python
# -*- coding: utf-8 -*-
# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
"""
Image tools interfaces
~~~~~~~~~~~~~~~~~~~~~~


"""

import os
import numpy as np
import nibabel as nb
import nilearn.image as nli
from glob import glob

from dipy.io import read_bvals_bvecs
from nipype import logging
from nipype.utils.filemanip import fname_presuffix
from nipype.interfaces.base import (isdefined, traits, TraitedSpec, BaseInterfaceInputSpec,
                                    SimpleInterface, File, InputMultiObject, OutputMultiObject)
from nipype.interfaces import fsl
from fmriprep.interfaces.images import (
    normalize_xform, demean, nii_ones_like, extract_wm, SignalExtraction, MatchHeader,
    FilledImageLike, DemeanImage, ValidateImage, TemplateDimensions)

from nipype.interfaces.afni.base import AFNICommand, AFNICommandInputSpec, AFNICommandOutputSpec

LOGGER = logging.getLogger('nipype.interface')


class SplitDWIsInputSpec(BaseInterfaceInputSpec):
    dwi_file = File(desc='the dwi image')
    bvec_file = File(desc='the bvec file')
    bval_file = File(desc='the bval file')
    b0_threshold = traits.Int(50, usedefault=True,
                              desc='Maximum b-value that can be considered a b0')


class SplitDWIsOutputSpec(TraitedSpec):
    dwi_files = OutputMultiObject(File(exists=True), desc='single volume dwis')
    bvec_files = OutputMultiObject(File(exists=True), desc='single volume bvecs')
    bval_files = OutputMultiObject(File(exists=True), desc='single volume bvals')
    b0_images = OutputMultiObject(File(exists=True), desc='just the b0s')
    b0_indices = traits.List(desc='list of original indices for each b0 image')


class SplitDWIs(SimpleInterface):
    input_spec = SplitDWIsInputSpec
    output_spec = SplitDWIsOutputSpec

    def _run_interface(self, runtime):
        split = fsl.Split(dimension='t', in_file=self.inputs.dwi_file)
        split_dwi_files = split.run().outputs.out_files

        split_bval_files, split_bvec_files = split_bvals_bvecs(
            self.inputs.bval_file, self.inputs.bvec_file, runtime)

        bvalues = np.loadtxt(self.inputs.bval_file)
        b0_indices = np.flatnonzero(bvalues < self.inputs.b0_threshold)
        b0_paths = [split_dwi_files[idx] for idx in b0_indices]

        self._results['dwi_files'] = split_dwi_files
        self._results['bval_files'] = split_bval_files
        self._results['bvec_files'] = split_bvec_files
        self._results['b0_images'] = b0_paths
        self._results['b0_indices'] = b0_indices.tolist()

        return runtime


class ConcatRPESplitsInputSpec(BaseInterfaceInputSpec):
    dwi_plus = OutputMultiObject(File(exists=True), desc='single volume dwis')
    bvec_plus = OutputMultiObject(File(exists=True), desc='single volume bvecs')
    bval_plus = OutputMultiObject(File(exists=True), desc='single volume bvals')
    b0_images_plus = OutputMultiObject(File(exists=True), desc='just the b0s')
    b0_indices_plus = traits.List(desc='list of original indices for each b0 image')
    hmc_affines_plus = OutputMultiObject(File(exists=True), desc='hmc_affine_transforms')
    hmc_affines_minus = OutputMultiObject(File(exists=True), desc='hmc_affine_transforms')
    template_plus_to_ref_affine = File(exists=True,
                                       desc='affine transform from hmc template to ref')
    template_plus_to_ref_warp = File(exists=True,
                                     desc='SDC warp for hmc template')
    dwi_minus = OutputMultiObject(File(exists=True), desc='single volume dwis')
    bvec_minus = OutputMultiObject(File(exists=True), desc='single volume bvecs')
    bval_minus = OutputMultiObject(File(exists=True), desc='single volume bvals')
    b0_images_minus = OutputMultiObject(File(exists=True), desc='just the b0s')
    b0_indices_minus = traits.List(desc='list of original indices for each b0 image')
    template_minus_to_ref_affine = File(exists=True,
                                        desc='affine transform from hmc template to ref')
    template_minus_to_ref_warp = File(exists=True,
                                      desc='SDC warp for hmc template')
    b0_ref_image = File(exists=True, desc='b0 reference image for coregistration')
    b0_ref_mask = File(exists=True, desc='mask for reference b0 image')


class ConcatRPESplitsOutputSpec(TraitedSpec):
    b0_ref_image = File(exists=True, desc='b0 reference image')
    b0_ref_mask = File(exists=True, desc='mask for reference b0 image')
    dwi_files = OutputMultiObject(File(exists=True), desc='single volume dwis')
    bvec_files = OutputMultiObject(File(exists=True), desc='single volume bvecs')
    bval_files = OutputMultiObject(File(exists=True), desc='single volume bvals')
    b0_images = OutputMultiObject(File(exists=True), desc='just the b0s')
    b0_indices = traits.List(desc='list of indices for each b0 image')
    to_dwi_ref_affines = OutputMultiObject(File(exists=True), desc='affines to b0 ref')
    to_dwi_ref_warps = OutputMultiObject(File(exists=True), desc='correcting warps to b0 ref')
    original_grouping = traits.List(desc='list of source series for each dwi')


class ConcatRPESplits(SimpleInterface):
    """Combine the outputs from the RPE series workflow into a SplitDWI-like object.

    Plus series goes first, indices are adjusted for minus to be globally correct.
    head motion affines are combined with to-ref affines and stored in dwi_to_ref_affines.
    """

    input_spec = ConcatRPESplitsInputSpec
    output_spec = ConcatRPESplitsOutputSpec

    def _run_interface(self, runtime):
        from .itk import compose_affines
        self._results['b0_ref_image'] = self.inputs.b0_ref_image
        self._results['b0_ref_mask'] = self.inputs.b0_ref_mask

        plus_images = self.inputs.dwi_plus
        plus_bvecs = self.inputs.bvec_plus
        plus_bvals = self.inputs.bval_plus
        plus_b0_images = self.inputs.b0_images_plus
        plus_b0_indices = self.inputs.b0_indices_plus
        plus_hmc_affines = self.inputs.hmc_affines_plus
        plus_hmc_to_ref_affine = self.inputs.template_plus_to_ref_affine
        plus_hmc_to_ref_warp = self.inputs.template_plus_to_ref_warp
        num_plus = len(plus_images)

        minus_images = self.inputs.dwi_minus
        minus_bvecs = self.inputs.bvec_minus
        minus_bvals = self.inputs.bval_minus
        minus_b0_images = self.inputs.b0_images_minus
        minus_b0_indices = self.inputs.b0_indices_minus
        minus_hmc_affines = self.inputs.hmc_affines_minus
        minus_hmc_to_ref_affine = self.inputs.template_minus_to_ref_affine
        minus_hmc_to_ref_warp = self.inputs.template_minus_to_ref_warp
        num_minus = len(minus_images)

        self._results['dwi_files'] = plus_images + minus_images
        self._results['bval_files'] = plus_bvals + minus_bvals
        self._results['bvec_files'] = plus_bvecs + minus_bvecs
        self._results['b0_images'] = plus_b0_images + minus_b0_images
        self._results['b0_indices'] = plus_b0_indices + [
            num_plus + idx for idx in minus_b0_indices]
        self._results['original_grouping'] = ['plus'] * num_plus + ['minus'] * num_minus

        # Create a list where each element is the warp for the DWI at the corresponding index
        if isdefined(plus_hmc_to_ref_warp) and isdefined(minus_hmc_to_ref_warp):
            self._results['to_dwi_ref_warps'] = [plus_hmc_to_ref_warp] * num_plus + \
                [minus_hmc_to_ref_warp] * num_minus

        # Combine the hmc affine with the to-reference affine
        combined_affines = []
        for affine_num, hmc_aff_plus in enumerate(plus_hmc_affines):
            combined_affines.append(
                compose_affines(self.inputs.b0_ref_image,
                                # Pop transforms from the stack: first hmc then to ref
                                [plus_hmc_to_ref_affine, hmc_aff_plus],
                                runtime.cwd + "/combined_hmc_affine_plus_%03d.mat" % affine_num))
        for affine_num, hmc_aff_minus in enumerate(minus_hmc_affines):
            combined_affines.append(
                compose_affines(self.inputs.b0_ref_image,
                                [minus_hmc_to_ref_affine, hmc_aff_minus],
                                runtime.cwd + "/combined_hmc_affine_minus_%03d.mat" % affine_num))
        self._results['to_dwi_ref_affines'] = combined_affines

        return runtime


class NiftiInfoInputSpec(BaseInterfaceInputSpec):
    in_file = File(exists=True, mandatory=True, desc='Input NIfTI file')


class NiftiInfoOutputSpec(TraitedSpec):
    voxel_size = traits.Tuple()


class NiftiInfo(SimpleInterface):
    input_spec = NiftiInfoInputSpec
    output_spec = NiftiInfoOutputSpec

    def _run_interface(self, runtime):
        in_file = self.inputs.in_file
        img = nb.load(in_file)
        self._results['voxel_size'] = tuple(img.header.get_zooms()[:3])
        return runtime


class IntraModalMergeInputSpec(BaseInterfaceInputSpec):
    in_files = InputMultiObject(File(exists=True), mandatory=True,
                                desc='input files')
    hmc = traits.Bool(True, usedefault=True)
    zero_based_avg = traits.Bool(True, usedefault=True)
    to_lps = traits.Bool(True, usedefault=True)


class IntraModalMergeOutputSpec(TraitedSpec):
    out_file = File(exists=True, desc='merged image')
    out_avg = File(exists=True, desc='average image')
    out_mats = OutputMultiObject(File(exists=True), desc='output matrices')
    out_movpar = OutputMultiObject(File(exists=True), desc='output movement parameters')


class IntraModalMerge(SimpleInterface):
    input_spec = IntraModalMergeInputSpec
    output_spec = IntraModalMergeOutputSpec

    def _run_interface(self, runtime):
        in_files = self.inputs.in_files
        if not isinstance(in_files, list):
            in_files = [self.inputs.in_files]

        # Generate output average name early
        self._results['out_avg'] = fname_presuffix(self.inputs.in_files[0],
                                                   suffix='_avg', newpath=runtime.cwd)

        if self.inputs.to_lps:
            in_files = [reorient(inf, newpath=runtime.cwd)
                        for inf in in_files]

        if len(in_files) == 1:
            filenii = nb.load(in_files[0])
            filedata = filenii.get_data()

            # magnitude files can have an extra dimension empty
            if filedata.ndim == 5:
                sqdata = np.squeeze(filedata)
                if sqdata.ndim == 5:
                    raise RuntimeError('Input image (%s) is 5D' % in_files[0])
                else:
                    in_files = [fname_presuffix(in_files[0], suffix='_squeezed',
                                                newpath=runtime.cwd)]
                    nb.Nifti1Image(sqdata, filenii.get_affine(),
                                   filenii.get_header()).to_filename(in_files[0])

            if np.squeeze(nb.load(in_files[0]).get_data()).ndim < 4:
                self._results['out_file'] = in_files[0]
                self._results['out_avg'] = in_files[0]
                # TODO: generate identity out_mats and zero-filled out_movpar
                return runtime
            in_files = in_files[0]
        else:
            magmrg = fsl.Merge(dimension='t', in_files=self.inputs.in_files)
            in_files = magmrg.run().outputs.merged_file
        mcflirt = fsl.MCFLIRT(cost='normcorr', save_mats=True, save_plots=True,
                              ref_vol=0, in_file=in_files)
        mcres = mcflirt.run()
        self._results['out_mats'] = mcres.outputs.mat_file
        self._results['out_movpar'] = mcres.outputs.par_file
        self._results['out_file'] = mcres.outputs.out_file

        hmcnii = nb.load(mcres.outputs.out_file)
        hmcdat = hmcnii.get_data().mean(axis=3)
        if self.inputs.zero_based_avg:
            hmcdat -= hmcdat.min()

        nb.Nifti1Image(
            hmcdat, hmcnii.get_affine(), hmcnii.get_header()).to_filename(
            self._results['out_avg'])

        return runtime


CONFORMATION_TEMPLATE = """\t\t<h3 class="elem-title">Anatomical Conformation</h3>
\t\t<ul class="elem-desc">
\t\t\t<li>Input T1w images: {n_t1w}</li>
\t\t\t<li>Output orientation: LPS</li>
\t\t\t<li>Output dimensions: {dims}</li>
\t\t\t<li>Output voxel size: {zooms}</li>
\t\t\t<li>Discarded images: {n_discards}</li>
{discard_list}
\t\t</ul>
"""

DISCARD_TEMPLATE = """\t\t\t\t<li><abbr title="{path}">{basename}</abbr></li>"""


class ConformInputSpec(BaseInterfaceInputSpec):
    in_file = File(exists=True, mandatory=True, desc='Input image')
    target_zooms = traits.Tuple(traits.Float, traits.Float, traits.Float,
                                desc='Target zoom information')
    target_shape = traits.Tuple(traits.Int, traits.Int, traits.Int,
                                desc='Target shape information')


class ConformOutputSpec(TraitedSpec):
    out_file = File(exists=True, desc='Conformed image')
    transform = File(exists=True, desc='Conformation transform')


class Conform(SimpleInterface):
    """Conform a series of T1w images to enable merging.

    Performs two basic functions:

    #. Orient to LPS (right-left, anterior-posterior, inferior-superior)
    #. Resample to target zooms (voxel sizes) and shape (number of voxels)
    """
    input_spec = ConformInputSpec
    output_spec = ConformOutputSpec

    def _run_interface(self, runtime):
        # Load image, orient as LPS
        fname = self.inputs.in_file
        orig_img = nb.load(fname)
        reoriented = to_lps(orig_img)

        # Set target shape information
        target_zooms = np.array(self.inputs.target_zooms)
        target_shape = np.array(self.inputs.target_shape)
        target_span = target_shape * target_zooms

        zooms = np.array(reoriented.header.get_zooms()[:3])
        shape = np.array(reoriented.shape[:3])

        # Reconstruct transform from orig to reoriented image
        ornt_xfm = nb.orientations.inv_ornt_aff(
            nb.io_orientation(reoriented.affine), orig_img.shape)
        # Identity unless proven otherwise
        target_affine = reoriented.affine.copy()
        conform_xfm = np.eye(4)
        # conform_xfm = np.diag([-1, -1, 1, 1])

        xyz_unit = reoriented.header.get_xyzt_units()[0]
        if xyz_unit == 'unknown':
            # Common assumption; if we're wrong, unlikely to be the only thing that breaks
            xyz_unit = 'mm'

        # Set a 0.05mm threshold to performing rescaling
        atol = {'meter': 1e-5, 'mm': 0.01, 'micron': 10}[xyz_unit]

        # Rescale => change zooms
        # Resize => update image dimensions
        rescale = not np.allclose(zooms, target_zooms, atol=atol)
        resize = not np.all(shape == target_shape)
        if rescale or resize:
            if rescale:
                scale_factor = target_zooms / zooms
                target_affine[:3, :3] = reoriented.affine[:3, :3].dot(np.diag(scale_factor))

            if resize:
                # The shift is applied after scaling.
                # Use a proportional shift to maintain relative position in dataset
                size_factor = target_span / (zooms * shape)
                # Use integer shifts to avoid unnecessary interpolation
                offset = (reoriented.affine[:3, 3] * size_factor - reoriented.affine[:3, 3])
                target_affine[:3, 3] = reoriented.affine[:3, 3] + offset.astype(int)

            data = nli.resample_img(reoriented, target_affine, target_shape).get_data()
            conform_xfm = np.linalg.inv(reoriented.affine).dot(target_affine)
            reoriented = reoriented.__class__(data, target_affine, reoriented.header)

        # Image may be reoriented, rescaled, and/or resized
        if reoriented is not orig_img:
            out_name = fname_presuffix(fname, suffix='_lps', newpath=runtime.cwd)
            reoriented.to_filename(out_name)
        else:
            out_name = fname

        transform = ornt_xfm.dot(conform_xfm)
        assert np.allclose(orig_img.affine.dot(transform), target_affine)

        mat_name = fname_presuffix(fname, suffix='.mat', newpath=runtime.cwd, use_ext=False)
        np.savetxt(mat_name, transform, fmt='%.08f')

        self._results['out_file'] = out_name
        self._results['transform'] = mat_name

        return runtime


class ConformDwiInputSpec(BaseInterfaceInputSpec):
    dwi_file = File(exists=True, mandatory=True, desc='dwi image')


class ConformDwiOutputSpec(TraitedSpec):
    dwi_file = File(exists=True, mandatory=True, desc='conformed dwi image')
    bvec_file = File(exists=True, mandatory=True, desc='conformed bvec file')
    bval_file = File(exists=True, mandatory=True, desc='conformed bval file')


class ConformDwi(SimpleInterface):
    """Conform a series of dwi images to enable merging.

    Performs three basic functions:

    #. Orient to LPS (right-left, anterior-posterior, inferior-superior)
    #. Flip bvecs accordingly
    #. Do nothing to the bvals

    Note: This is not as nuanced as fmriprep's version
    """
    input_spec = ConformDwiInputSpec
    output_spec = ConformDwiOutputSpec

    def _run_interface(self, runtime):
        fname = self.inputs.dwi_file
        out_fname = fname_presuffix(fname, suffix='_lps', newpath=runtime.cwd)
        bvec_fname = fname_presuffix(fname, suffix=".bvec", use_ext=False)
        bval_fname = fname_presuffix(fname, suffix=".bval", use_ext=False)
        out_bvec_fname = fname_presuffix(bvec_fname, suffix='_lps', newpath=runtime.cwd)
        input_img = nb.load(fname)
        input_axcodes = nb.aff2axcodes(input_img.affine)
        # Is the input image oriented how we want?
        new_axcodes = ('L', 'P', 'S')
        if not input_axcodes == new_axcodes:
            # Re-orient
            input_orientation = nb.orientations.axcodes2ornt(input_axcodes)
            desired_orientation = nb.orientations.axcodes2ornt(new_axcodes)
            transform_orientation = nb.orientations.ornt_transform(
                        input_orientation, desired_orientation)
            reoriented_img = input_img.as_reoriented(transform_orientation)
            reoriented_img.to_filename(out_fname)

            # Flip the bvecs
            bvec_array = np.loadtxt(bvec_fname)
            if not bvec_array.shape[0] == transform_orientation.shape[0]:
                raise ValueError("Unrecognized bvec format")
            output_array = np.zeros_like(bvec_array)
            for this_axnum, (axnum, flip) in enumerate(transform_orientation):
                output_array[this_axnum] = bvec_array[int(axnum)] * flip
            np.savetxt(out_bvec_fname, output_array, fmt="%.8f ")
            self._results['bvec_file'] = out_bvec_fname
            self._results['dwi_file'] = out_fname

        else:
            self._results['dwi_file'] = fname
            self._results['bvec_file'] = bvec_fname

        self._results['bval_file'] = bval_fname

        return runtime


def split_bvals_bvecs(bval_file, bvec_file, runtime):
    """Split bvals and bvecs into one text file per image."""
    bvals, bvecs = read_bvals_bvecs(bval_file, bvec_file)
    split_bval_files = []
    split_bvec_files = []
    for nsample, (bval, bvec) in enumerate(zip(bvals[:, None], bvecs)):
        bval_fname = fname_presuffix(bval_file, suffix='_%04d' % nsample, newpath=runtime.cwd)
        bvec_fname = fname_presuffix(bvec_file, suffix='_%04d' % nsample, newpath=runtime.cwd)
        np.savetxt(bval_fname, bval)
        np.savetxt(bvec_fname, bvec)
        split_bval_files.append(bval_fname)
        split_bvec_files.append(bvec_fname)

    return split_bval_files, split_bvec_files


def reorient(in_file, newpath=None):
    """Reorient Nifti files to LPS."""
    out_file = fname_presuffix(in_file, suffix='_lps', newpath=newpath)
    to_lps(nb.load(in_file)).to_filename(out_file)
    return out_file


def to_lps(input_img):
    new_axcodes = ("L", "P", "S")
    input_axcodes = nb.aff2axcodes(input_img.affine)
    # Is the input image oriented how we want?
    if not input_axcodes == new_axcodes:
        # Re-orient
        input_orientation = nb.orientations.axcodes2ornt(input_axcodes)
        desired_orientation = nb.orientations.axcodes2ornt(new_axcodes)
        transform_orientation = nb.orientations.ornt_transform(input_orientation,
                                                               desired_orientation)
        reoriented_img = input_img.as_reoriented(transform_orientation)
        return reoriented_img
    else:
        return input_img