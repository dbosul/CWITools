from astropy.io import fits
from astropy import units as u
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_scales
from cwitools import coordinates, utils
from datetime import datetime
from scipy.stats import sigmaclip
from tqdm import tqdm

import argparse
import cwitools
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pyregion
import sys


def get_mask2d(reg, fits3d):
    data3d, hdr3d = fits3d[0].data, fits3d[0].header
    data2d, hdr2d = np.mean(data3d, axis=0), coordinates.get_header2d(hdr3d)

    mask2d = np.zeros_like(data2d, dtype=bool)
    wcs2d = WCS(hdr2d)
    yscale, xscale = proj_plane_pixel_scales(wcs2d)
    yscale = (yscale * u.deg).to(u.arcsec).value
    xscale = (xscale * u.deg).to(u.arcsec).value

    Ny, Nx = data2d.shape
    y, x = np.arange(Ny), np.arange(Nx)
    yy, xx = np.meshgrid(x, y)

    src_rads = [(x.coord_list[2] * u.deg).to(u.arcsec).value for x in reg]

    for i, shape in enumerate(reg.as_imagecoord(hdr2d)):
        src_y, src_x, src_rad = shape.coord_list

        src_y -= 0.5
        src_x -= 0.5

        src_rad_as = src_rads[i] / 2

        rr_px = np.sqrt(np.power(xx - src_x, 2) + np.power(yy - src_y, 2))
        rr_as = np.sqrt(np.power((xx - src_x) * xscale, 2) + np.power((yy - src_y) * yscale, 2))

        mask2d[rr_as <= src_rad_as] = 1

    return mask2d

def main():
    parser = argparse.ArgumentParser(description='Experimental sky subtraction.')
    parser.add_argument('clist',
                        type=str,
                        help='The input id list.'
    )
    parser.add_argument('ctype',
                        type=str,
                        help='The input cube type.'
    )
    parser.add_argument('srcmask',
                        type=str,
                        help='DS9 region (.reg) file of areas to exclude'
    )
    parser.add_argument('-var',
                        type=str,
                        help='The input var cube type.',
                        default=None
    )
    parser.add_argument('-poly_k',
                        type=int,
                        help='Degree of poly1d fit to residuals of slice-by-slice model',
                        default=None
    )
    parser.add_argument('-sclip',
                        type=float,
                        help='Sigma-clipping factor to apply when computing master sky.',
                        default=3
    )
    parser.add_argument('-ext',
                        type=str,
                        help='Output file extension.',
                        default=".ss.fits"
    )
    parser.add_argument('-log',
                        type=str,
                        help='Log file to store output in.',
                        default=None
    )
    parser.add_argument('-silent',
                        help='Set flag to suppress standard output',
                        action='store_true'
    )
    args = parser.parse_args()

    #Set global parameters
    cwitools.silent_mode = args.silent
    cwitools.log_file = args.log

    #Get command that was issued
    argv_string = " ".join(sys.argv)
    cmd_string = "python " + argv_string + "\n"

    #Summarize script usage
    timestamp = datetime.now()

    infostring = """\n{0}\n{1}\n\tCWI_SKYSUB:\n
    \t\tCLIST = {1}
    \t\tCTYPE = {2}
    \t\tSRCMASK = {3}
    \t\tVAR= {4}
    \t\tPOLY_K = {5}
    \t\tEXT = {5}
    \t\tLOG = {6}
    \t\tSILENT = {7}\n\n""".format(timestamp, cmd_string, args.clist, args.srcmask,
    args.var, args.poly_k, args.ext, args.log, args.silent)

    #Output info string
    utils.output(infostring)

    clist = utils.parse_cubelist(args.clist)
    file_list = utils.find_files(
        clist["ID_LIST"],
        clist["INPUT_DIRECTORY"],
        args.ctype,
        clist["SEARCH_DEPTH"]
    )

    if args.var != None:
        var_file_list = utils.find_files(
            clist["ID_LIST"],
            clist["INPUT_DIRECTORY"],
            args.var,
            clist["SEARCH_DEPTH"]
        )
        usevar=True
    else:
        usevar=False

    fits_all = []
    specs_all = []
    msks_all = []

    pyreg = pyregion.open(args.srcmask)

    #STEP 1: Create master median sky spectrum from masked input object cubes
    utils.output("\tMaking master sky....\n")
    wav_axis = None
    for file_in in tqdm(file_list):

        fits_in = fits.open(file_in)

        if wav_axis is None:
            wav_axis = coordinates.get_wav_axis(fits_in[0].header)

        msk2d = get_mask2d(pyreg, fits_in)

        data = fits_in[0].data
        for yi in range(data.shape[1]):
            for xi in range(data.shape[2]):
                if not msk2d[yi, xi]:
                    specs_all.append(data[:, yi, xi])

        msks_all.append(msk2d)
        fits_all.append(fits_in)

    specs_all = np.array(specs_all)
    master_sky = np.zeros_like(wav_axis)
    for i, wav_i in enumerate(wav_axis):
        sky_clipped = sigmaclip(specs_all[:, i],
            low=args.sclip,
            high=args.sclip
        ).clipped
        master_sky[i] = np.median(sky_clipped)

    fig, ax = plt.subplots(1, 1)
    ax.plot(wav_axis, master_sky, 'k-')
    fig.show()
    input("")
    
    N = specs_all.shape[0]

    #Error on median = 1.235 * sigma / sqrt(N)
    master_sky_err = 1.253 * np.std(specs_all, axis=0) / np.sqrt(N)
    master_sky_var = np.power(master_sky_err, 2)

    #STEP 2: Scale and subtract spectrum from each cube, using slice-by-slice scaling
    utils.output("\tScaling and subtracting...\n")
    for i, fits_in in tqdm(enumerate(fits_all)):

        cube = fits_in[0].data
        msk2d = get_mask2d(pyreg, fits_in)
        wav_axis = coordinates.get_wav_axis(fits_in[0].header)

        if usevar:
           var_fits = fits.open(var_file_list[i])

        for j in range(cube.shape[2]):

            use_px = msk2d[:, j]
            med_slice_spec = np.median(cube[:, :, j], axis=1)

            scaling_factors = med_slice_spec / master_sky
            scale_med = np.median(scaling_factors)

            #Calculate model and variance
            sky_model = scale_med * master_sky
            sky_model_var = (scale_med**2) * master_sky_var
            residuals = med_slice_spec - sky_model
            #Fit polynomial to residuals
            if args.poly_k != None:


                coeff, covar = np.polyfit(wav_axis, residuals, 2, full=False, cov=True)
                polymodel = np.poly1d(coeff)

                #Calculate variance on polynomial
                polymodel_var = np.zeros_like(sky_model)
                for m in range(covar.shape[0]):
                    var_m = 0
                    for l in range(covar.shape[1]):
                        var_m += np.power(wav_axis, args.poly_k - l) * covar[l, m] / np.sqrt(covar[m, m])

                    polymodel_var += var_m**2

                sky_model += polymodel(wav_axis)
                sky_model_var += polymodel_var


            # fig, axes = plt.subplots(2, 1)
            # axes[0].plot(wav_axis, med_slice_spec, 'k.-')
            # axes[0].plot(wav_axis, sky_model, 'r-')
            # axes[1].plot(wav_axis, residuals, 'k.-')
            # fig.show()
            # plt.waitforbuttonpress()
            # plt.close()
            for yi in range(cube.shape[1]):
                cube[:, yi, j] -= sky_model

                if usevar:
                    var_fits[0].data[:, yi, j] += sky_model_var

        file_out = file_list[i].replace(".fits", args.ext)
        fits_in[0].data = cube

        fits_in.writeto(file_out, overwrite=True)
        utils.output("\tSaved {0}\n".format(file_out))

        if usevar:
            var_file_out = file_out.replace(".fits", ".var.fits")
            var_fits.writeto(var_file_out, overwrite=True)
            utils.output("\tSaved {0}\n".format(var_file_out))
if __name__=="__main__": main()
