"""Tools for subtracting sources and background."""
from astropy import units as u
from astropy.modeling import models, fitting
from astropy.nddata import Cutout2D
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_scales
from cwitools import coordinates
from cwitools.modeling import sigma2fwhm
from photutils import DAOStarFinder
from scipy.ndimage.filters import generic_filter
from scipy.ndimage.measurements import center_of_mass
from scipy.stats import sigmaclip, tstd
from tqdm import tqdm

import numpy as np
import os
import pyregion
import sys

def psf_sub_1d(fits_in, pos, fit_rad=2, sub_rad=15, slice_rad=4,
wl_window=150, wmasks=[], var_cube=[], slice_axis=2):
    """Models and subtracts a point-source on a slice-by-slice basis.

    Args:
        fits_in (astropy FITS object): Input data cube/FITS.
        pos (float tuple): (x,y) position of the source to subtract.
        fit_rad (float): Inner radius, in arcsec, to use for fitting PSF within a slice.
        sub_rad (float): Outer radius, in arcsec, over which to subtract PSF within a slice.
        slice_rad (int): Number of slices to subtract over, as distance from the
            slice indicated by the `pos' argument.
        wl_window (float): Size of white-light window (in Angstrom) to use.
            This is the window used to form a white-light image centered
            on each wavelength layer. Default: 150A.
        wmasks (list): List of wavelength tuples to exclude when making
            white-light images. Use to exclude nebular emission or sky lines.
        slice_axis (int): Which axis represents the slices of the image.
            For KCWI default data cubes, slice_axis = 2. For PCWI data cubes,
            slice_axis = 1.
        var_cube (numpy.ndarray): Variance cube associated with input. Optional.
            Method returns propagated variance if given.
    Returns:
        numpy.ndarray: PSF-subtracted data cube
        numpy.ndarray: PSF model data
        numpy.ndarray: (if var_cube given) Propagated variance cube

    """
    #Extract data + meta-data
    cube, hdr = fits_in[0].data, fits_in[0].header
    cube = np.nan_to_num(cube, nan=0, posinf=0, neginf=0)

    usevar = var_cube != []

    #Get in-slice plate scale in arcseconds
    wcs = WCS(hdr)
    px_scales = proj_plane_pixel_scales(wcs)
    if slice_axis == 2:
        inslice_px_scale = (px_scales[1] * u.deg).to(u.arcsec).value
    elif slice_axis == 1:
        inslice_px_scale = (px_scales[2] * u.deg).to(u.arcsec).value

    #Convert radii from arcseconds to pixels
    fit_rad_px = fit_rad / inslice_px_scale
    sub_rad_px = sub_rad / inslice_px_scale

    #Rotate so slice axis is 2 (Clockwise by 90 deg)
    if slice_axis == 1:
        cube = np.rot90(cube, axes=(1, 2), k=1)

    z, y, x = cube.shape
    Z, Y, X = np.arange(z), np.arange(y), np.arange(x)
    x0, y0 = pos

    x0 = int(round(x0))
    y0 = int(round(y0))

    cd3_3 = hdr["CD3_3"]
    wav_axis = coordinates.get_wav_axis(hdr)
    psf_model = np.zeros_like(cube)

    #Create wavelength masked based on input
    zmask = np.ones_like(wav_axis, dtype=bool)
    for (w0, w1) in wmasks:
        zmask[(wav_axis > w0) & (wav_axis < w1)] = 0
    nzmax = np.count_nonzero(zmask)

    #Create masks for fitting and subtracting PSF
    fit_mask = np.abs(Y - y0) <= fit_rad_px
    sub_mask = np.abs(Y - y0) <= sub_rad_px

    #Get minimum and maximum slices for subtraction
    slice_min = max(0, x0 - slice_rad)
    slice_max = min(x - 1, x0 + slice_rad + 1)


    #Loop over wavelength first to minimize repetition of wl-mask calculation
    for j, wav_j in enumerate(wav_axis):

        #Get initial width of white-light bandpass in px
        wl_width_px = wl_window / cd3_3

        #Create initial white-light mask, centered on j with above width
        wl_mask = zmask & (np.abs(Z - j) <= wl_width_px / 2)

        #Grow until minimum number of valid wavelength layers included
        while np.count_nonzero(wl_mask) < min(nzmax, wl_window / cd3_3):
            wl_width_px += 2
            wl_mask = zmask & (np.abs(Z - j) <= wl_width_px / 2)

        #Iterate over slices and perform subtraction
        for slice_i in range(slice_min, slice_max):

            #Get 1D profile of current slice/wav and WL profile
            j_prof = cube[j, :, slice_i].copy()

            N_wl = np.count_nonzero(wl_mask)
            wl_prof = np.sum(cube[wl_mask, :, slice_i], axis=0) / N_wl

            if np.count_nonzero(~sub_mask) > 5:
                wl_prof -= np.median(wl_prof[~sub_mask])

            if np.sum(wl_prof[fit_mask]) / np.std(wl_prof) < 7:
                continue

            #Exclude negative WL pixels from scaling fit
            fit_mask_i = fit_mask #& (wl_prof > 0)

            #Calculate scaling factor
            scale_factors = j_prof[fit_mask_i] / wl_prof[fit_mask_i]

            scale_factor_med = np.mean(scale_factors)
            scale_factor = scale_factor_med

            #Create WL model
            wl_model = scale_factor * wl_prof

            psf_model[j, sub_mask, slice_i] += wl_model[sub_mask]

            if usevar:
                wl_var = np.sum(var_cube[wl_mask, :, slice_i], axis=0) / N_wl**2
                wl_model_var = (scale_factor**2) * wl_var
                var_cube[j, sub_mask, slice_i] += wl_model_var


    #Subtract PSF model
    cube -= psf_model

    #Rotate back if data was rotated at beginning (Clockwise by 270 deg)
    if slice_axis == 1:
        cube = np.rot90(cube, axes=(1, 2), k=3)
        psf_model = np.rot90(cube, axes=(1, 2), k=3)

    if usevar:
        return cube, psf_model, var_cube

    else:
        return cube, psf_model

def psf_sub_2d(inputfits, pos, fit_rad=1.5, sub_rad=5.0, wl_window=200,
wmasks=[], recenter=True, recenter_rad=5, var_cube=[]):
    """Models and subtracts point-sources in a 3D data cube.

    Args:
        inputfits (astrop FITS object): Input data cube/FITS.
        fit_rad (float): Inner radius, used for fitting PSF.
        sub_rad (float): Outer radius, used to subtract PSF.
        pos (float tuple): (x,y) position of the source to subtract.
        recenter (bool): Recenter the input (x, y) using the centroid within a
            box of size recenter_box, arcseconds.
        recenter_rad(float): Radius of circle used to recenter PSF, in arcsec.
        wl_window (int): Size of white-light window (in Angstrom) to use.
            This is the window used to form a white-light image centered
            on each wavelength layer. Default: 200A.
        wmasks (list): List of wavelength tuples to exclude when making
            white-light images. Use to exclude nebular emission or sky lines.
        var_cube (numpy.ndarray): Variance cube associated with input. Optional.
            Method returns propagated variance if given.

    Returns:
        numpy.ndarray: PSF-subtracted data cube
        numpy.ndarray: PSF model cube
        numpy.ndarray: (if var_cube given) Propagated variance cube

    """

    #Open fits image and extract info
    xP, yP = pos
    cube = inputfits[0].data
    header = inputfits[0].header
    z, y, x = cube.shape
    Z, Y, X = np.arange(z), np.arange(y), np.arange(x)
    wav = coordinates.get_wav_axis(header)
    wcs = WCS(header)
    cd3_3 = header["CD3_3"]

    usevar = (var_cube != [])

    #Get plate scales in arcseconds and Angstrom
    pxScales = proj_plane_pixel_scales(wcs)
    xScale, yScale = (pxScales[:2] * u.deg).to(u.arcsecond)
    zScale = (pxScales[2] * u.meter).to(u.angstrom).value
    recenter_rad_px = recenter_rad / xScale.value

    #Convert fitting & subtracting radii from arcsecond/Angstrom to pixels
    fit_rad_px = fit_rad/xScale.value
    sub_rad_px = sub_rad/xScale.value
    wl_window_px = int(round(wl_window / zScale))

    #Remove NaN values
    cube = np.nan_to_num(cube,nan=0.0,posinf=0,neginf=0)

    #Create cube for subtracted cube and for model of psf
    psf_cube = np.zeros_like(cube)

    #Make mask canvas
    msk2D = np.zeros((y, x))

    #Create white-light image
    zmask = np.ones_like(wav, dtype=bool)
    for (w0, w1) in wmasks:
        zmask[(wav >= w0) & (wav <= w1)] = 0
    nzmax = np.count_nonzero(zmask)

    #Create WL image
    wl_img = np.sum(cube[zmask], axis=0)

    #Create meshgrid of distance from source
    YY, XX = np.meshgrid(X - xP, Y - yP)
    RR = np.sqrt(XX**2 + YY**2)

    #Reposition source if requested
    if recenter:

        recenter_img = wl_img.copy()
        recenter_img[RR > recenter_rad_px] = 0
        xP, yP = center_of_mass(recenter_img)

        #Update after recentering
        YY, XX = np.meshgrid(X - xP, Y - yP)
        RR = np.sqrt(XX**2 + YY**2)

    #Get boolean masks for
    fit_mask = RR <= fit_rad_px
    sub_mask = (RR <= sub_rad_px)

    #Run through wavelength layers
    for i, wav_i in enumerate(wav):

        #Get initial width of white-light bandpass in px
        wl_width_px = wl_window / cd3_3

        #Create initial white-light mask, centered on j with above width
        wl_mask = zmask & (np.abs(Z - i) <= wl_width_px / 2)

        #Grow mask until minimum number of valid wavelength layers is included
        while np.count_nonzero(wl_mask) < min(nzmax, wl_window / cd3_3):
            wl_width_px += 2
            wl_mask = zmask & (np.abs(Z - i) <= wl_width_px / 2)

        #Get current layer and do median subtraction (after sigclipping)
        layer_i = cube[i]

        #Get white-light image and do the same thing
        N_wl = np.count_nonzero(wl_mask)
        wlimg_i = np.sum(cube[wl_mask], axis=0) / N_wl

        #Background subtract if any background available
        if np.count_nonzero(~sub_mask) >= 5:

            layer_i_bg = sigmaclip(layer_i[~sub_mask], low=3, high=3).clipped
            layer_i -= np.median(layer_i_bg)

            wlimg_i_bg =  sigmaclip(wlimg_i[~sub_mask], low=3, high=3).clipped
            wlimg_i -= np.median(wlimg_i_bg)

        #Calculate scaling factors
        sfactors = layer_i[fit_mask] / wlimg_i[fit_mask]

        #Get scaling factor, A
        A = np.median(sfactors)

        #Set to zero for bad values
        if A < 0 or np.isinf(A) or np.isnan(A): A = 0

        #Add to PSF model
        psf_cube[i][sub_mask] += A * wlimg_i[sub_mask]

        if usevar:
            wlimg_i_var = np.sum(var_cube[wl_mask], axis=0) / N_wl**2
            var_cube[i][sub_mask] += (A**2) * wlimg_i_var[sub_mask]

    #Subtract 3D PSF model
    sub_cube = cube - psf_cube

    #Return subtracted data alongside model
    if usevar:
        return sub_cube, psf_cube, var_cube
    else:
        return sub_cube, psf_cube

def psf_sub_all(inputfits, fit_rad=1.5, sub_rad=5.0, reg=None, pos=None,
recenter=True, auto=7, wl_window=200, wmasks=[], slice_axis=2, method='2d',
slice_rad=3, var_cube=[]):
    """Models and subtracts point-sources in a 3D data cube.

    Args:
        inputfits (astrop FITS object): Input data cube/FITS.
        fit_rad (float): Inner radius, used for fitting PSF.
        sub_rad (float): Outer radius, used to subtract PSF.
        reg (str): Path to a DS9 region file containing sources to subtract.
        pos (float tuple): (x,y) position of the source to subtract.
        auto (float): SNR above which to automatically detect/subtract sources.
            Note: One of the parameters reg, pos, or auto must be provided.
        wl_window (int): Size of white-light window (in Angstrom) to use.
            This is the window used to form a white-light image centered
            on each wavelength layer. Default: 200A.
        method (str): Method of PSF subtraction.
            '1d': Subtract PSFs on slice-by-slice basis with 1D models.
            '2d': Subtract PSFs using a 2D PSF model.
        wmask (int tuple): Wavelength region to exclude from white-light images.
        slice_axis (int): Which axis represents the slices of the image.
            For KCWI default data cubes, slice_axis = 2. For PCWI data cubes,
            slice_axis = 1. Only relevant if using 1d subtraction.
        slice_rad (int): Number of slices from central slice over which to
            subtract PSF for each source when using 1d method. Default is 3.
        var_cube (numpy.ndarray): Variance cube associated with input. Optional.
            Method returns propagated variance if given.

    Returns:
        numpy.ndarray: PSF-subtracted data cube
        numpy.ndarray: PSF model cube
        numpy.ndarray: (if var_cube given) Propagated variance cube


    Examples:

        To subtract point sources from an input cube using a DS9 region file:

        >>> from astropy.io import fits
        >>> from cwitools import psf_subtract
        >>> myregfile = "mysources.reg"
        >>> myfits = fits.open("mydata.fits")
        >>> sub_cube, psf_model = psf_subtract(myfits, reg = myregfile)

        To subtract using automatic source detection with photutils, and a
        source S/N ratio >5:

        >>> sub_cube, psf_model = psf_subtract(myfits, auto = 5)

        Or to subtract a single source from a specific location (x,y)=(21.1,34.6):

        >>> sub_cube, psf_model = psf_subtract(myfits, pos=(21.1, 34.6))

    """
    #Open fits image and extract info
    cube = inputfits[0].data
    header = inputfits[0].header
    w, y, x = cube.shape
    W, Y, X = np.arange(w), np.arange(y), np.arange(x)
    wav = coordinates.get_wav_axis(header)

    usevar = var_cube != []

    #Get WCS information
    wcs = WCS(header)
    pxScales = proj_plane_pixel_scales(wcs)

    #Remove NaN values
    cube = np.nan_to_num(cube,nan=0.0,posinf=0,neginf=0)

    #Create cube for subtracted cube and for model of psf
    psf_cube = np.zeros_like(cube)
    sub_cube = cube.copy()

    #Make mask canvas
    msk2D = np.zeros((y, x))

    #Create wavelength mask for white-light image
    zmask = np.ones_like(wav, dtype=bool)
    for (w0, w1) in wmasks:
        zmask[(wav >= w0) & (wav <= w1)] = 0

    #Create white-light image
    wlImg = np.sum(cube[zmask], axis=0)

    #Get list of sources, method depending on input
    sources = []

    #If a single source is given, use that.
    if pos != None:
        sources = [pos]

    #If region file is given
    elif reg != None:

        if os.path.isfile(reg):
            regFile = pyregion.open(reg)
        else:
            raise FileNotFoundError("Region file could not be found: %s"%reg)

        for src in regFile:
            ra, dec, pa = src.coord_list
            xP, yP, wP = wcs.all_world2pix(ra, dec, header["CRVAL3"], 0)
            xP = float(xP)
            yP = float(yP)
            sources.append((xP, yP))

    #Otherwise, use the automatic method
    else:

        #Get standard deviation in WL image (sigmaclip to remove bright sources)
        stddev = np.std(sigmaclip(wlImg, low=3, high=3).clipped)

        #Run source finder
        daofind = DAOStarFinder(fwhm=8.0, threshold=auto * stddev)
        autoSrcs = daofind(wlImg)

        #Get list of peak values
        peaks = list(autoSrcs['peak'])

        #Make list of sources
        sources = []
        for i in range(len(autoSrcs['xcentroid'])):
            sources.append((autoSrcs['xcentroid'][i], autoSrcs['ycentroid'][i]))

        #Sort according to peak value (this will be ascending)
        peaks, sources = zip(*sorted(zip(peaks, sources)))

        #Cast back to list
        sources = list(sources)

        #Reverse to get descending order (brightest sources first)
        sources.reverse()

    #Run through sources
    psf_model = np.zeros_like(cube)
    sub_cube = cube.copy()

    for pos in sources:

        if method == '1d':

            res = psf_sub_1d(inputfits,
                pos = pos,
                fit_rad = fit_rad,
                sub_rad = sub_rad,
                wl_window = wl_window,
                wmasks = wmasks,
                slice_axis = slice_axis,
                slice_rad = slice_rad,
                var_cube = var_cube
            )

        elif method == '2d':

            res = psf_sub_1d(inputfits,
                pos = pos,
                fit_rad = fit_rad,
                sub_rad = sub_rad,
                wl_window = wl_window,
                wmasks = wmasks,
                var_cube = var_cube
            )

        if usevar:
            sub_cube, model_P, var_cube = res

        else:
            sub_cube, model_P = res
        #Update FITS data and model cube
        inputfits[0].data = sub_cube
        psf_model += model_P

    if usevar:
        return sub_cube, psf_model, var_cube
    else:
        return sub_cube, psf_model

def bg_sub(inputfits, method='polyfit', poly_k=1, median_window=31,
zmasks=[(0, 0)], zunit='A'):
    """
    Subtracts extended continuum emission / scattered light from a cube

    Args:
        cubePath (str): Path to the data cube to be subtracted.
        method (str): Which method to use to model background
            'polyfit': Fits polynomial to the spectrum in each spaxel (default.)
            'median': Subtract the spatial median of each wavelength layer.
            'medfilt': Model spectrum in each spaxel by median filtering it.
            'noiseFit': Model noise in each z-layer and subtract mean.
        poly_k (int): The degree of polynomial to use for background modeling.
        median_window (int): The filter window size to use if median filtering.
        zmask (int tuple): Wavelength region to mask, given as tuple of indices.
        zunit (str): If using zmask, indices are given in these units.
            'A': Angstrom (default)
            'px': pixels
        saveModel (bool): Set to TRUE to save background model cube.
        fileExt (str): File extension to use for output (Default: .bs.fits)

    Returns:
        NumPy.ndarray: Background-subtracted cube
        NumPy.ndarray: Cube containing background model which was subtracted.

    Examples:

        To model the background with 1D polynomials for each spaxel's spectrum,
        using a quadratic polynomial (k=2):

        >>> from cwitools import bg_subtract
        >>> from astropy.io import fits
        >>> myfits = fits.open("mydata.fits")
        >>> bgsub_cube, bgmodel_cube = bg_subtract(myfits, method='polfit', poly_k=2)

    """

    #Load header and data
    header = inputfits[0].header.copy()
    cube = inputfits[0].data.copy()
    varcube = np.zeros_like(cube)

    W = coordinates.get_wav_axis(header)
    z, y, x = cube.shape
    xySize = cube[0].size
    maskZ = False
    modelC = np.zeros_like(cube)

    #Get empty regions mask
    mask2D = np.sum(cube, axis=0) == 0

    #Convert zmask to pixels if given in angstrom
    useZ = np.ones_like(W, dtype=bool)
    for (z0, z1) in zmasks:
        print(z0,z1)
        if zunit == 'A': z0, z1 = coordinates.get_indices(z0, z1, header)
        useZ[z0:z1] = 0

    #Subtract background by fitting a low-order polynomial
    if method == 'polyfit':

        #Track progress % using n
        n = 0

        #Run through spaxels and subtract low-order polynomial
        for yi in range(y):
            for xi in range(x):

                n += 1
                p = 100*float(n)/xySize
                sys.stdout.write('%5.2f percent complete\r'%p)
                sys.stdout.flush()

                #Extract spectrum at this location
                spectrum = cube[:, yi, xi].copy()

                coeff, covar = np.polyfit(W[useZ], spectrum[useZ], poly_k,
                    full=False,
                    cov=True
                )

                polymodel = np.poly1d(coeff)

                #Get background model
                bgModel = polymodel(W)

                if mask2D[yi, xi] == 0:

                    cube[:, yi, xi] -= bgModel

                    #Add to model
                    modelC[:, yi, xi] += bgModel

                    for m in range(covar.shape[0]):
                        var_m = 0
                        for l in range(covar.shape[1]):
                            var_m += np.power(W, poly_k - l) * covar[l, m] / np.sqrt(covar[m, m])
                        varcube[:, yi, xi] += var_m**2
                        
    #Subtract background by estimating it with a median filter
    elif method == 'medfilt':

        #Get +/- 5px windows around masked region, if mask is set
        if z1 > 0:

            #Get size of window region used to interpolate (minimum 5 to get median)
            nw = max(5, (z1-z0))

            #Get left and right index of window regions
            a = max(0, z0-nw)
            b = min(z, z1+nw)

            #Get two z mid-points which we will use for calculating line slope/intercept
            ZA = (a+z0)/2.0
            ZB = (b+z1)/2.0

            maskZ = True

        #Track progress % using n
        n = 0

        for yi in range(y):
            for xi in range(x):
                n += 1
                p = 100*float(n)/xySize
                sys.stdout.write('%5.2f percent complete\r'%p)
                sys.stdout.flush()

                #Extract spectrum at this location
                spectrum = cube[:, yi, xi].copy()

                #Fill in masked region with smooth linear interpolation
                if maskZ:

                    #Calculate slope and intercept
                    YA = np.mean(spectrum[a:z0]) if (z0-a) < 5 else np.median(spectrum[a:z0])
                    YB = np.mean(spectrum[z1:b]) if (b-z1) < 5 else np.median(spectrum[z1:b])
                    m = (YB-YA)/(ZB-ZA)
                    c = YA - m*ZA

                    #Get domain for masked pixels
                    ZZ = np.arange(z0, z1+1)

                    #Apply mask
                    spectrum[z0:z1+1] = m*ZZ + c

                #Get median filtered spectrum as background model
                bgModel = generic_filter(spectrum, np.median, size=median_window, mode='reflect')

                if mask2D[yi, xi] == 0:

                    #Subtract from data
                    cube[:, yi, xi] -= bgModel

                    #Add to model
                    modelC[:, yi, xi] += bgModel

    #Subtract layer-by-layer by fitting noise profile
    elif method == 'noiseFit':
        fitter = fitting.SimplexLSQFitter()
        medians = []
        for zi in range(z):

            #Extract layer
            layer = cube[zi]
            layerNonZ = layer[~mask2D]

            #Get median
            median = np.median(layerNonZ)
            stddev = np.std(layerNonZ)
            trimmed_stddev = tstd(layerNonZ, limits=(-3*stddev, 3*stddev))
            trimmed_median = np.median(layerNonZ[np.abs(layerNonZ-median) < 3*trimmed_stddev])

            medians.append(trimmed_median)
        medians = np.array(medians)
        bgModel0 = models.Polynomial1D(degree=2)

        bgModel1 = fitter(bgModel0, W[useZ], medians[useZ])
        for i, wi in enumerate(W):
            cube[i][~mask2D] -= bgModel1(wi)
            modelC[i][~mask2D] = bgModel1(wi)

    #Subtract using simple layer-by-layer median value
    elif method == "median":

        dataclipped = sigmaclip(cube).clipped

        for zi in range(z):
            modelC[zi][mask2D == 0] = np.median(dataclipped[zi][mask2D == 0])
        modelC = medfilt(modelC, kernel_size=(3, 1, 1))
        cube -= modelC

    return cube, modelC, varcube
