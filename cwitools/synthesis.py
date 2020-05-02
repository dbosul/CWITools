"""Tools for generating scientific products from the extracted signal."""
from astropy import units as u
from astropy import convolution
from astropy.cosmology import WMAP9 as cosmo
from astropy.cosmology import WMAP9
from astropy.modeling import models, fitting
from astropy.nddata import Cutout2D
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_scales
import reproject

from cwitools import coordinates, measurement, utils
from cwitools.modeling import fwhm2sigma
from scipy.stats import sigmaclip
from skimage import measure

import numpy as np
import os
import pyregion
import warnings

def whitelight(fits_in,  wmask=[], var_cube=None, mask_sky=False, wavgood=True):
    """Get white-light image from cube.

    Input can be ~astropy.io.fits.HDUList, ~astropy.io.fits.PrimaryHDU or
    ~astropy.io.fits.ImageHDU. If HDUList given, PrimaryHDU will be used.

    Returned objects will be of same type as input.

    Args:
        fits_in (astropy HDU / HDUList): Input HDU/HDUList with 3D data.
        wmask (list): List of wavelength tuples to exclude when making
            white-light image. Use to exclude nebular emission or sky lines.
        var (Numpy.ndarray): Variance cube corresponding to input cube
        mask_sky (bool): Set to TRUE to mask some known bright sky lines.
        wavgood (bool): Set to TRUE to limit to WAVGOOD region.
        
    Returns:
        HDU / HDUList*: White-light image + header
        HDU / HDUList*: Esimated variance on WL image.
        *Return type matches type of fits_in argument.

    """

    #Extract data + meta-data
    hdu = utils.extractHDU(fits_in)
    data, header = hdu.data, hdu.header

    #Filter data for bad values
    data = np.nan_to_num(data, nan=0, posinf=0, neginf=0)

    #Get new header object for 2D output
    header2d = coordinates.get_header2d(header)
    header2d_var = header2d.copy()

    #Get wavelength axis for masking
    wav_axis = coordinates.get_wav_axis(header)

    #Create wavelength masked based on input
    if wavgood:
        wmask.append([0, header["WAVGOOD0"]])
        wmask.append([header["WAVGOOD1"], wav_axis[-1]])

    #Apply mask
    zmask = np.zeros_like(wav_axis, dtype=bool)
    for (w0, w1) in wmask:
        zmask[(wav_axis > w0) & (wav_axis < w1)] = 1

    #Add sky mask if requested
    if mask_sky:
        skymask = utils.get_skymask(header)
        zmask = zmask | skymask #OR combine

    #Sum over WL wavelengths
    wl_img = np.sum(data[~zmask], axis=0)

    #Get variance estimate, whether variance given or not
    if var_cube is not None:
        var = np.nan_to_num(var_cube, nan=0, posinf=0, neginf=0)
        wl_var = np.sum(var_cube[~zmask], axis=0)
    else:
        wl_var = np.var(data[~zmask], axis=0)

    #Unit conversions
    if 'BUNIT' in header.keys():
        bunit = utils.get_bunit(header)
        if not 'electrons' in bunit:
            bunit2d = utils.multiply_bunit(bunit, 'angstrom')
            bunit2d_var = utils.multiply_bunit(bunit2d, bunit2d)
            
            flam2f = coordinates.get_pxsize_angstrom(header)
            wl_img *= flam2f
            wl_var *= flam2f**2

        #Update header
        header2d['BUNIT'] = bunit2d
        header2d_var['BUNIT'] = bunit2d_var

    #Get return type (HDU or HDUList)
    wl_hdu = utils.matchHDUType(fits_in, wl_img, header2d)
    wl_var_hdu = utils.matchHDUType(fits_in, wl_var, header2d_var)

    return wl_hdu, wl_var_hdu



def pseudo_nb(fits_in, wav_center=None, wav_width=None, pos=None, fit_rad=2,
sub_rad=None, var_cube=None):
    """Create a pseudo-Narrow-Band (pNB) image from a data cube.

    Input can be ~astropy.io.fits.HDUList, ~astropy.io.fits.PrimaryHDU or
    ~astropy.io.fits.ImageHDU. If HDUList given, PrimaryHDU will be used.

    Returned objects will be of same type as input.

    Args:
        fits_in (astropy HDU or HDUList): Input HDU/HDUList with 3D data.
        wav_center (float): The central wavelength of the pNB, in Angstrom.
        wav_width (float): The bandwidth of the pNB, in Angstrom.
        pos (float tuple): Provide the x,y location the source to subtract.
            Leave empty to skip white-light subtraction.
        fit_rad (float): Radius (px) to use for scaling the PSF.
        sub_rad (float): Radius (px) to use when subtracting PSF.
        var_cube (NumPy.ndarray): Variance cube associated with input cube.
            Provide to obtain variance estimates on pNB (and WL) images.

    Returns:
        HDU / HDUList*: pseudo-Narrowband image.
        HDU / HDUList*: The variance on the pNB image.
        HDU / HDUList*: White-light / broad-band image.
        HDU / HDUList*: The variance on the white-light image
        *Return type matches type of fits_in argument.

    """

    #Extract data and header from relevant HDU
    hdu = utils.extractHDU(fits_in)
    int_cube, header3d = hdu.data, hdu.header

    #Get 2D header for output
    header2d = coordinates.get_header2d(header3d)
    header2d_var = header2d.copy()

    #Filter out bad values
    int_cube = np.nan_to_num(int_cube, nan=0, posinf=0, neginf=0)

    #Get wavelength axis
    wav_axis = coordinates.get_wav_axis(header3d)

    #Get parameters for NB image and WL image
    pnb_wA = wav_center - wav_width / 2
    pnb_wB = wav_center + wav_width / 2

    #Get indices of NB image
    A, B = coordinates.get_indices(pnb_wA, pnb_wB, header3d)

    #Handle out of bounds errors or warnings
    if B <= 0 or A >= int_cube.shape[0] - 1:
        raise ValueError("Requested pNB bandpass outside cube range.")

    if A < 0:
        warnings.warn("Requested pNB bandpass is clipped by cube range.")
        A = 0

    if B > int_cube.shape[0]-1:
        warnings.warn("Requested pNB bandpass is clipped by cube range.")
        B = -1

    #Create the narrowband image
    nb_img = np.sum(int_cube[A:B], axis=0)

    #Estimate or sum the variance
    if var_cube is not None:
        var_cube[np.isnan(var_cube)] = 0
        nb_var = np.sum(var_cube[A:B], axis=0)
    else:
        nb_var = np.var(var_cube[A:B], axis=0)

    #Unit conversions
    if 'BUNIT' in header.keys():
        bunit = utils.get_bunit(header)
        if not 'electrons' in bunit:
            bunit2d = utils.multiply_bunit(bunit, 'angstrom')
            bunit2d_var = utils.multiply_bunit(bunit2d, bunit2d)
            
            flam2f = coordinates.get_pxsize_angstrom(header)
            wl_img *= flam2f
            wl_var *= flam2f**2

        #Update header
        header2d['BUNIT'] = bunit2d
        header2d_var['BUNIT'] = bunit2d_var

    #Get WL data and variance
    wl_img, wl_var = whitelight(int_cube, wmask=[[pnb_wA, pnb_wB]], var=var)

    #Subtract source if a position is provided
    if pos is not None:

        #Get masks for scaling + subtracting
        rr_qso = coordinates.get_rmesh(wl_hdu, pos[0], pos[1])
        fitMask = rr_qso <= fit_rad
        subMask = rr_qso <= sub_rad

        #Find scaling factor
        scale_factors = sigmaclip(NB[fitMask] / WL[fitMask]).clipped
        scale = np.median(scale_factors)

        #Scale WL image subtract
        wl_img *= scale
        nb_img[subMask] -= wl_img[subMask]

        #Propagate error
        wl_var *= (scale**2)
        nb_var[subMask] += wl_var[subMask]

    #Convert all output to HDUs
    nb_out = utils.matchHDUType(fits_in, nb_img, header2d)
    nb_var_out = utils.matchHDUType(fits_in, nb_var, header2d)
    wl_out = utils.matchHDUType(fits_in, wl_img, header2d)
    wl_var_out = utils.matchHDUType(fits_in, wl_var, header2d)

    return nb_out, nb_var_out, wl_out, wl_var_out

def radial_profile(fits_in, pos, rmin=-1, rmax=-1, nbins=10, scale='lin',
mask=None, var_map=None, runit='px', redshift=None):
    """Measures a radial profile from a surface brightness (SB) map.

    Input can be ~astropy.io.fits.HDUList, ~astropy.io.fits.PrimaryHDU or
    ~astropy.io.fits.ImageHDU. If HDUList given, PrimaryHDU will be used.

    Args:
        fits_in (HDU or HDUList): Input HDU/HDUList containing SB map.
        pos (float tuple): The center of the profile in image coordinates.
        rmin (float): The minimum radius, in units determined by runit.
        rmax (float): The maximum radius, in units determined by runit.
        nbins (int): The number of radial bins between rmin and rmax to use.
        scale (str): The scale for the radial bins.
            'lin' makes bins equal size in linear space.
            'log' makes bins equal size in log space.
        mask (NumPy.ndarray): A 2D binary mask of regions to exclude.
        var (NumPy.ndarray): A 2D map of variance, used for error propagation.
        runit (str): The unit of rmin and rmax. Can be 'pkpc' or 'px'
            'pkpc' Proper kiloparsec, redshift must also be provided.
            'px' pixels (i.e. distance in image coordinates)

    Returns:
        astropy.io.fits.TableHDU: Table containing columns 'radius', 'sb_avg',
            and 'sb_err' (i.e. the radial sb profile)

    """
    #Extract input data
    hdu = utils.extractHDU(fits_in)
    sb_map, header2d = hdu.data, hdu.header

    #Check mask and set to empty if none given
    mask = np.zeros_like(rr) if mask is none else mask

    if runit == 'pkpc':
        rr = coordinates.get_rmesh(fits_in, x, y, unit='arcsec')
        if redshift is None:
            raise ValueError("Redshift must be provided if runit='pkpc'")
        else:
            pkpc_per_arcsec = cosmo.kpc_proper_per_arcmin(redshift) / 60.0
        rr *= pkpc_per_arcsec

    #Get min and max
    rmin = np.min(rr) if rmin == -1 else rmin
    rmax = np.max(rr) if rmax == -1 else rmax

    #Get r array
    if scale == 'lin':
        r_edges = np.linspace(rmin, rmax, nbins)

    elif scale == 'log':
        r_edges_log = np.linspace(np.log10(rmin), np.log10(rmax), nsteps)
        r_edges = np.power(10, r_edges_log)

    else:
        raise ValueError("'scale' argument can only be 'lin' or 'log'")

    #Create array for radial profile and error
    rprof = np.zeros_like(r_edges[:-1])
    rprof[:] = np.NaN
    rprof_err = np.copy(rprof)
    r_centers = np.copy(rprof)

    #Loop over edges and calculate radial profile
    for i in range(r_edges[:-1].size):

        #Get binary mask of useable spaxels in this radial bin
        rmask = (rr >= r_edges[i]) & (rr < r_edges[i+1]) & ~mask

        #Skip empty bins
        nmask = np.count_nonzero(rmask)
        if nmask == 0: continue

        sb_avg = np.sum(sb_map[rmask]) / nmask

        #Calculate variance, from given variance or sb map
        if var_map is None:
            sb_var = np.var(sb_map[rmask])
        else:
            sb_var = np.sum(var_map[rmask]) / nmask**2

        sb_err = np.sqrt(sb_var)

        rprof[i] = sb_avg
        rprof_err[i] = sb_err
        rcenters[i] = (r_edges[i] + r_edges[i+1]) / 2.0

    col1 = fits.Column(
        name='radius',
        format='D',
        array=rcenters,
        unit=runit
    )
    col2 = fits.Column(
        name='sb_avg',
        format='D',
        array=rprof,
        unit=utils.get_bunit(header2d)
    )
    col3 = fits.Column(
        name='sb_err',
        format='D',
        array=rprof_err,
        unit=utils.get_bunit(header2d)
    )
    table_hdu = fits.TableHDU.from_columns([col1, col2, col3])
    return table_hdu

def obj_sb(fits_in, obj_cube, obj_id, var_cube=None):
    """Get surface brightness map from segmented 3D objects.

    Input can be ~astropy.io.fits.HDUList, ~astropy.io.fits.PrimaryHDU or
    ~astropy.io.fits.ImageHDU. If HDUList given, PrimaryHDU will be used.

    Returned objects will be of same type as input.

    Args:
        fits_in (astropy HDU or HDUList): Input HDU/HDUList with 3D data.
        obj_cube (NumPy.ndarray): Data cube containing labelled 3D regions.
        obj_id (list or int): ID or list of IDs of objects to include.
        var_cube (NumPy.ndarray): Data cube containing 3D variance estimate.

    Returns:
        HDU / HDUList*: Surface brightness map and header.
        HDU / HDUList*: Variance on surface brightness map, with header.
        *Return type matches fits_in.
    """
    #Extract data and header
    hdu = utils.extractHDU(fits_in)
    int_cube, header3d = hdu.data, hdu.header

    #Get conversion to SB
    flam2sb = coordinates.get_flam2sb(header3d)

    #Create 3D mask from object cube and IDs
    msk_cube = np.zeros_like(obj_cube, dtype=bool)

    if type(obj_id) == int:
        msk_cube = obj_cube == obj_id

    elif type(obj_id) == list and np.all(np.array(a) == int):
        msk_cube = np.zeros_like(obj_cube, dtype=bool)
        for oid in obj_ids:
            msk_cube[obj_cube == oid] = 1

    else:
        raise TypeError("obj_id must be an integer or list of integers.")

    #Mask non-object data and sum SB map
    int_cube[~msk_cube] = 0
    fluxmap = np.sum(int_cube, axis=0)
    sbmap = fluxmap * flam2sb

    #Get 2D header and update units
    header2d = coordinates.get_header2d(header3d)
    header2d['BUNIT'] = header3d['BUNIT'].replace('FLAM', 'SB')

    #Get output of same FITS/HDU type as input
    sb_out = utils.matchHDUType(fits_in, sbmap, header2d)

    #Calculate and return with variance map if varcube provided
    if type(var_cube) == type(obj_cube):
        var_cube[~msk_cube] = 0
        varmap = np.sum(var_cube, axis=0) * (flam2sb**2)
        sb_var_out = utils.matchHDUType(fits_in, varmap, header2d)
        return sb_out, sb_var_out

    else:
        return sb_out

def obj_spec(fits_in, obj_cube, obj_id, var_cube=None, limit_z=True):
    """Get 1D spectrum of segmented 3D objects.

    Input can be ~astropy.io.fits.HDUList, ~astropy.io.fits.PrimaryHDU or
    ~astropy.io.fits.ImageHDU. If HDUList given, PrimaryHDU will be used.

    Args:
        fits_in (astropy HDU or HDUList): Input HDU/HDUList with 3D data.
        obj_cube (NumPy.ndarray): Data cube containing labelled 3D regions.
        obj_id (list or int): ID or list of IDs of objects to include.
        var_cube (NumPy.ndarray): Data cube containing 3D variance estimate.
        limit_z (bool): Set to False to use full spectrum in each object spaxel.

    Returns:
        astropy.io.fits.TableHDU: Table with columns 'wav' (wavelength), 'flux',
            and - if var_cube was provided - 'flux_err'.
    """
    #Extract relevant data and header
    hdu = utils.extractHDU(fits_in)
    int_cube, header3d = hdu.data, hdu.header

    #Create mask
    msk_cube = np.zeros_like(obj_cube, dtype=bool)

    #Mask 3D object mask of desired objects
    if type(obj_id) == int:
        msk_cube = obj_cube == obj_id

    elif type(obj_id) == list and np.all(np.array(a) == int):
        msk_cube = np.ones_like(obj_cube, dtype=bool)
        for oid in obj_ids:
            msk_cube[obj_cube == oid] = 0

    else:
        raise TypeError("obj_id must be an integer or list of integers.")

    #Extend mask along full z-axis if desired
    if not limit_z:
        msk2d = np.max(msk_cube, axis=0)
        msk_cube = np.zeros_like(obj_cube).T
        msk_cube[msk2d] = 1
        msk_cube = msk_cube.T

    #Mask data and sum over spatial axes
    int_cube[msk_cube] = 0
    spec1d = np.sum(int_cube, axis=(1, 2))

    #Get wavelength array
    wav_axis = coordinates.get_wav_axis(header3d)

    #Get 1D header and create HDU-like object matching input type
    header1d = coordinates.get_header1d(header3d)
    spec1d_out = utils.matchHDUType(fits_in, spec1d, header1d)

    col1 = fits.Column(
        name='wav',
        format='D',
        array=wav_axis,
        unit=header3d["CUNIT3"]
    )
    col2 = fits.Column(
        name='flux',
        format='E',
        array=spec1d,
        unit=header3d['BUNIT']
    )

    #Propagate variance and add error column if provided
    if var_cube is not None:
        var_cube[mask_cube] = 0
        spec1d_var = np.sum(var_cube, axis=(1, 2))
        spec1d_err = np.sqrt(spec1d_var)
        col3 = fits.Column(
            name='flux_err',
            format='E',
            array=spec1d_err,
            unit=header3d['BUNIT']
        )
        table_hdu = fits.TableHDU.from_columns([col1, col2, col3])
    else:
        table_hdu = fits.TableHDU.from_columns([col1, col2])

    return table_hdu

def obj_moments(fits_in, obj_cube, obj_id, var_cube=None, unit='kms'):
    """Creates 2D maps of 1st and 2nd z-moments for 3D objects.

    Input can be ~astropy.io.fits.HDUList, ~astropy.io.fits.PrimaryHDU or
    ~astropy.io.fits.ImageHDU. If HDUList given, PrimaryHDU will be used.

    Returned objects will be of same type as input.

    Args:
        fits_in (astropy HDU or HDUList): Input HDU/HDUList with 3D data.
        obj_cube (NumPy.ndarray): Data cube containing labelled 3D regions.
        obj_id (list or int): ID or list of IDs of objects to include.
        var_cube (NumPy.ndarray): Data cube containing 3D variance estimate.
        unit (str): Desired output unit.
            'kms' - kilometers per second
            'wav' - wavelength units (same as input z-axis)

    Returns:
        HDU / HDUList*: First moment (velocity) map, with header
        HDU / HDUList*: Error on first moment map, with header
        HDU / HDUList*: Second moment (dispersion) map, with header
        HDU / HDUList*: Error on second moment map, with header
        *Return type matches fits_in.
    """
    #Extract relevant data and header
    hdu = utils.extractHDU(fits_in)
    int_cube, header3d = hdu.data, hdu.header

    #Validate unit selection
    if unit not in ['kms', 'wav']:
        raise ValueError("'unit' argument can only be 'wav' or 'kms'")

    #Get 2D header for output
    header2d = coordinates.get_header2d(header3d)

    #Create mask
    msk_cube = np.zeros_like(obj_cube, dtype=bool)

    #Get wavelength axis
    wav_axis = coordinates.get_wav_axis(header3d)

    #Mask 3D object mask of desired objects
    if type(obj_id) == int:
        msk_cube = obj_cube == obj_id

    elif type(obj_id) == list and np.all(np.array(a) == int):
        msk_cube = np.ones_like(obj_cube, dtype=bool)
        for oid in obj_ids:
            msk_cube[obj_cube == oid] = 0

    else:
        raise TypeError("obj_id must be an integer or list of integers.")

    #Create 2D map of object spaxels
    msk2d = np.max(msk_cube, axis=0)

    #Create blank arrays for moment maps
    m1_map = np.zeros_like(msk2d, dtype=float)
    m2_map = np.zeros_like(msk2d, dtype=float)

    #Initialize as NaNs
    m1_map[:] = np.NaN
    m2_map[:] = np.NaN

    #Also create arrays for moment map error
    m1_err_map = np.copy(m1_map)
    m2_err_map = np.copy(m2_map)

    #Loop over spaxels and calculate moments
    for yi in range(int_cube.shape[1]):
        for xj in range(int_cube.shape[2]):

            msk_ij = msk_cube[:, yi, xi]

            #Skip empty spaxels
            if np.count_nonzero(msk_ij) == 0: continue

            #Extract wavelength domain and spectrum for this spaxel
            wav_ij = wav_axis[msk_ij]
            spc_ij = int_cube[msk_ij, yi, xi]
            var_ij = [] if var_cube == [] else var_cube[msk_ij, yi, xi]

            #Calculate first moment
            m1, m1_err = measurement.first_moment(wav_ij, spc_ij,
                method = 'basic',
                y_var = var_ij,
                get_err = True
            )

            m1_map[yi, xi] = m1
            m1_err_map[yi, xi] = m1_err

            #Calculate second moment
            m2, m2_err = measurement.second_moment(wav_ij, spc_ij,
                m1 = m1,
                y_var = var_ij,
                get_err = True
            )

            m2_map[yi, xi] = m2
            m2_err_map[yi, xi] = m2_err

    #If velocity units requested
    if unit.lower() == 'kms':

        #Get flux-weighted average wavelength
        spec1d = obj_spec(fits_in, obj_cube, obj_id)
        m1_ref = measurement.first_moment(wav_axis, spec1d, method='basic')

        #Convert maps to velocity, in km/s
        cfactor = 3e5 / m1_ref #speed of light
        m1_map = cfactor * (m1_map - m1_ref)
        m1_err_map *= cfactor
        m2_map *= cfactor
        m2_err_map *= cfactor

        header2d['BUNIT'] = 'km/s'

    else:

        header2d['BUNIT'] = header3d['CUNIT3']

    #Add each to its own HDU or HDUList structure
    m1_out = utils.matchHDUType(fits_in, m1_map, header2d)
    m1_err_out = utils.matchHDUType(fits_in, m1_err_map, header2d)
    m2_out = utils.matchHDUType(fits_in, m2_map, header2d)
    m2_err_out = utils.matchHDUType(fits_in, m2_err_map, header2d)

    #Return all
    return m1_out, m1_err_out, m2_out, m2_err_out


import pdb
def cylindrical(fits_in,center,seg_mask=None,ellipticity=1.,pa=0.,nr=None,npa=None,r_range=None,
                pa_range=[0,360],dr=None,dpa=None,c_radec=False,
                clean=True,tmp_dir='cwitools/',compress=True,redshift=None,cosmo=WMAP9):
    """Resample a cube in cartesian coordinate to cylindrical coordinate (with ellipticity).
        This function can be used to project 3D cubes to the 2D spectra in lambda-r space.

    Args:
        fits_in (astropy HDU or HDUList): Input HDU/HDUList with 3D data.
        
        center (float tuple): Center of the cylindrical projection, in pixel coordinate or 
            [RA, DEC] if c_radec is True. 
            
        seg_mask (float arrray): Segment mask in 2D (typically generated by SExtractor) that
            masks additional continuum sources that do not corresponds to the central object.
            
        ellipticity (float): Axis-ratio. 
        
        pa (float): Position angle of the *MAJOR* axis that is east of the north direction. 
        
        nr (int): Number of pixels of the post-projection cubes in the radial direction.
            Default: The closest integer that makes the size of individual pixels to 
            be 0.3 arcsec in the major axis. 
            
        npa (int): Number of pixels of the post-projection cubes in the angular direction. 
            Default: The closest integer that makes the size of individual pixels to 
            be 1 degree. 
            
        r_range (float tuple): radial bins in major axies inside which the projection is applied. 
            Default: 0 to the minimum radius to include all signals in the input cube.
            
        pa_range (float tuple): position angles bins inside which the projections is applied.
            Default: [0,360]
            
        dr (float): radial size per pixel.
        
        dpa (float): PA size per pixel.
        
        c_radec (bool): If set, the center is specified using [RA, DEC] instead of pixels. 
        
        clean (bool): Clean up the intermediate files.
        
        tmp_dir (str): Specify where to store the intermediate files.
        
        compress (bool): Remove axis with only 1 pixel. This risks of losing WCS information on 
            the corresponding axis, but is convenient when using DS9. 
            
        cosmo (FlatLambdaCDM): The cosmology to use, as one of Astropy's
            cosmologies (astropy.cosmology.FlatLambdaCDM). Default is WMAP9.
            
        redshift (float): If set, an additional WCS is added to represent rest-wavelength and pkpc.

    Returns:
        HDU / HDUList*: Post-projection image/cube.
        HDU / HDUList*: Coverage image.
        *Return type matches type of fits_in argument.
    """
    
    delete_dir=False
    if not os.path.exists(tmp_dir):
        os.makedirs(tmp_dir)
        delete_dir=True
    
    hdu = utils.extractHDU(fits_in)
    
    # 0 - the original HDU
    hdu0=hdu
    hdu0.data=np.nan_to_num(hdu0.data,nan=0,posinf=0,neginf=0)
    wcs0=WCS(hdu0.header)
    sz=hdu0.data.shape

    # masking
    mask_3d=np.zeros(hdu0.shape,dtype=bool)
    if not seg_mask is None:
        for i in range(mask_3d.shape[0]):
            mask_3d[i,:,:]=np.bitwise_or(mask_3d[i,:,:],seg_mask)
        
    hdu0_mask=hdu0.copy()
    hdu0_mask.data[mask_3d==True]=0
        
    # Skewing
    hdu1=hdu0_mask.copy()
    hdr1=hdu1.header
    if ellipticity!=1:
        CD0=np.array([[hdr1['CD1_1'],hdr1['CD1_2']],
            [hdr1['CD2_1'],hdr1['CD2_2']]])
        rot=np.radians(pa)
        ROT=np.array([[np.cos(rot),-np.sin(rot)],
            [np.sin(rot),np.cos(rot)]])
        CD_rot=np.matmul(ROT,CD0)
        CD_shr=CD_rot
        if ellipticity<1:
            CD_shr[0,0]=CD_rot[0,0]/ellipticity
            CD_shr[0,1]=CD_rot[0,1]/ellipticity
        elif ellipticity>1:
            CD_shr[0,0]=CD_rot[0,0]*ellipticity
            CD_shr[0,1]=CD_rot[0,1]*ellipticity
        
        ROT=np.array([[np.cos(-rot),-np.sin(-rot)],
            [np.sin(-rot),np.cos(-rot)]])
        CD1=np.matmul(ROT,CD_shr)
        
        hdr1['CD1_1']=CD1[0,0]
        hdr1['CD1_2']=CD1[0,1]
        hdr1['CD2_1']=CD1[1,0]
        hdr1['CD2_2']=CD1[1,1]
    
    
    # Shift hdu to the south pole
    wcs1=WCS(hdr1)
    hdu2=hdu1.copy()
    hdr2=hdu2.header
    if c_radec==True:
        tmp=wcs1.wcs_world2pix(center[0],center[1]+0.3/3600.,0,0)
        ref_pix=[float(tmp[0]),float(tmp[1])]
        
        center_ad=center
        tmp=wcs0.wcs_world2pix(center[0],center[1],0,0)
        center_pix=[float(tmp[0]),float(tmp[1])]
    else:
        center_pix=center
        tmp=wcs0.all_pix2world(center[0],center[1],0,0)
        center_ad=[float(tmp[0]),float(tmp[1])]
        tmp=wcs1.all_world2pix(tmp[0],tmp[1]+0.3/3600.,0,0)
        ref_pix=[float(tmp[0]),float(tmp[1])]


    hdr2['CRPIX1']=ref_pix[0]+1
    hdr2['CRPIX2']=ref_pix[1]+1
    hdr2['CRVAL1']=0.
    hdr2['CRVAL2']=-90+0.3/3600.
    wcs2=WCS(hdr2)
    

    # Calculate the x dimension of the final cube
    if (npa==None) and (dpa==None):
        dx0=1.
        nx0=int(np.round((pa_range[1]-pa_range[0])/dx0))
        pa_range[1]=pa_range[0]+dx0*nx0
    else:
        if npa!=None:
            nx0=int(npa)
            dx0=(pa_range[1]-pa_range[0])/nx0
        if dpa!=None:
            nx0=int(np.round(pa_range[1]-pa_range[0]/dpa))
            dx0=dpa
            pa_range[1]=pa_range[0]+dx0*nx0
    
    # Split too large pixels
    if dx0>1:
        nx=int(dx0)*nx0
        dx=(pa_range[1]-pa_range[0])/nx
    else:
        nx=nx0
        dx=dx0
    # Montage can't handle DPA>180, splitting. Also need padding to avoid edge effect. 
    nx3=np.round(nx/3)
    nx3=np.array([nx3,nx3,nx-2*nx3])
    xr3=np.zeros((2,3))
    xr3[0,0]=pa_range[1]
    xr3[1,0]=pa_range[1]-nx3[0]*dx
    xr3[0,1]=xr3[1,0]
    xr3[1,1]=pa_range[1]-(nx3[0]+nx3[1])*dx
    xr3[0,2]=xr3[1,1]
    xr3[1,2]=pa_range[1]-np.sum(nx3)*dx
    nx3=nx3+10
    xr3[0,:]=xr3[0,:]+5*dx
    xr3[1,:]=xr3[1,:]-5*dx


    # Calculate the y dimension
    if r_range==None:
        p_corner_x=np.array([0,0,sz[2]-1,sz[2]-1])
        p_corner_y=np.array([0,sz[1]-1,sz[1]-1,0])
        p_corner_z=np.array([0,0,0,0])
        tmp=wcs2.wcs_pix2world(p_corner_x,p_corner_y,p_corner_z,0)
        a_corner=tmp[0]
        d_corner=tmp[1]
        r_corner=np.abs(d_corner+90)*3600.
        r_range=[0,np.max(r_corner)]
    if (nr==None) and (dr==None):
        dy=0.3
        ny=int(np.round((r_range[1]-r_range[0])/dy))
        r_range[1]=r_range[0]+dy*ny
    else:
        if nr!=None:
            ny=int(nr)
            dy=(r_range[1]-r_range[0])/ny
        if dr!=None:
            ny=int(np.round((r_range[1]-r_range[0])/dr))
            dy=dr
            r_range[1]=r_range[0]+dy*ny

    
    # Set up headers
    hdr3_1=hdu2.header.copy()
    hdr3_1['NAXIS1']=int(nx3[0])
    hdr3_1['NAXIS2']=ny
    hdr3_1['CTYPE1']='RA---CAR'
    hdr3_1['CTYPE2']='DEC--CAR'
    hdr3_1['CUNIT1']='deg'
    hdr3_1['CUNIT2']='deg'
    hdr3_1['CRVAL1']=xr3[0,0]
    hdr3_1['CRVAL2']=0.
    hdr3_1['CRPIX1']=0.5
    hdr3_1['CRPIX2']=(90.-r_range[0])/(dy/3600.)+0.5
    hdr3_1['CD1_1']=-dx
    hdr3_1['CD2_1']=0.
    hdr3_1['CD1_2']=0.
    hdr3_1['CD2_2']=dy/3600.
    hdr3_1['LONPOLE']=180.
    hdr3_1['LATPOLE']=0.
    hdr3_1fn=tmp_dir+'/cart2cyli_hdr3_1.hdr'
    hdr3_1.totextfile(hdr3_1fn,overwrite=True)

    hdr3_2=hdr3_1.copy()
    hdr3_2['NAXIS1']=int(nx3[1])
    hdr3_2['CRVAL1']=xr3[0,1]
    hdr3_2fn=tmp_dir+'/cart2cyli_hdr3_2.hdr'
    hdr3_2.totextfile(hdr3_2fn,overwrite=True)

    hdr3_3=hdr3_1.copy()
    hdr3_3['NAXIS1']=int(nx3[2])
    hdr3_3['CRVAL1']=xr3[0,2]
    hdr3_3fn=tmp_dir+'/cart2cyli_hdr3_3.hdr'
    hdr3_3.totextfile(hdr3_3fn,overwrite=True)

    # reproject
    cube3_1,area3_1=reproject.reproject_interp(hdu2,hdr3_1)
    cube3_2,area3_2=reproject.reproject_interp(hdu2,hdr3_2)
    cube3_3,area3_3=reproject.reproject_interp(hdu2,hdr3_3)

    data4=np.zeros((cube3_1.shape[0],cube3_1.shape[1],
            cube3_1.shape[2]+cube3_2.shape[2]+cube3_3.shape[2]-30))
    data4[:,:,0:cube3_1.shape[2]-10]=cube3_1[:,:,5:cube3_1.shape[2]-5]
    data4[:,:,cube3_1.shape[2]-10:cube3_1.shape[2]+cube3_2.shape[2]-20]=cube3_2[:,:,5:cube3_2.shape[2]-5]
    data4[:,:,cube3_1.shape[2]+cube3_2.shape[2]-20:cube3_1.shape[2]+cube3_2.shape[2]+cube3_3.shape[2]-30]=cube3_3[:,:,5:cube3_3.shape[2]-5]
    data4[data4==0]=np.nan

    area4=np.zeros((area3_1.shape[0],area3_1.shape[1],
            area3_1.shape[2]+area3_2.shape[2]+area3_3.shape[2]-30))
    area4[:,:,0:area3_1.shape[2]-10]=area3_1[:,:,5:area3_1.shape[2]-5]
    area4[:,:,area3_1.shape[2]-10:area3_1.shape[2]+area3_2.shape[2]-20]=area3_2[:,:,5:area3_2.shape[2]-5]
    area4[:,:,area3_1.shape[2]+area3_2.shape[2]-20:area3_1.shape[2]+area3_2.shape[2]+area3_3.shape[2]-30]=area3_3[:,:,5:area3_3.shape[2]-5]
    area4=np.nan_to_num(area4)
    area4[~np.isfinite(data4)]=0

    # Resample to final grid
    hdr5=hdr3_1.copy()
    hdr5['NAXIS1']=nx0
    hdr5['CTYPE1']='PA'
    hdr5['CTYPE2']='Radius'
    hdr5['CNAME1']='PA'
    hdr5['CNAME2']='Radius'
    hdr5['CRVAL1']=pa_range[1]
    hdr5['CRVAL2']=r_range[0]
    hdr5['CRPIX2']=0.5
    hdr5['CUNIT2']='arcsec'
    hdr5['CD1_1']=-dx0
    hdr5['CD2_2']=dy
    hdr5['C2C_ORA']=(center_ad[0],'RA of origin')
    hdr5['C2C_ODEC']=(center_ad[1],'DEC of origin')
    hdr5['C2C_OX']=(center_pix[0]+1,'X of origin')
    hdr5['C2C_OY']=(center_pix[1]+1,'Y of origin')
    hdr5['C2C_E']=(ellipticity,'Axis raio')
    hdr5['C2C_EPA']=(pa,'PA of the major axis')
    # 2nd WCS
    hdr5['CTYPE1A']='PA'
    hdr5['CTYPE2A']='Radius'
    hdr5['CTYPE3A']=hdr5['CTYPE3']
    hdr5['CNAME1A']='PA'
    hdr5['CNAME2A']='Radius'
    hdr5['CNAME3A']=hdr5['CNAME3']
    hdr5['CRVAL1A']=pa_range[1]
    hdr5['CRVAL2A']=r_range[0]
    hdr5['CRVAL3A']=hdr5['CRVAL3']
    hdr5['CRPIX1A']=hdr5['CRPIX1']
    hdr5['CRPIX2A']=0.5
    hdr5['CRPIX3A']=hdr5['CRPIX3']
    hdr5['CUNIT1A']=hdr5['CUNIT1']
    hdr5['CUNIT2A']=hdr5['CUNIT2']
    hdr5['CUNIT3A']=hdr5['CUNIT3']
    hdr5['CD1_1A']=-dx0
    hdr5['CD2_2A']=dy
    hdr5['CD3_3A']=hdr5['CD3_3']
    

    if redshift>0:
        a_dis=(cos.arcsec_per_kpc_proper(redshift)).value
        
        hdr5['CRVAL2A']=r_range[0]/a_dis
        hdr5['CUNIT2A']='kpc'
        hdr5['CD2_2A']=dy/a_dis
        
        hdr5['CRVAL3A']=hdr5['CRVAL3']/(1+redshift)
        hdr5['CD3_3A']=hdr5['CD3_3']/(1+redshift)



    ahdr5=hdr5.copy()
    if ~exact_area:
        ahdr5['NAXIS']=2
        del ahdr5['NAXIS3']
        del ahdr5['CTYPE3']
        del ahdr5['CUNIT3']
        del ahdr5['CNAME3']
        del ahdr5['CRVAL3']
        del ahdr5['CRPIX3']
        del ahdr5['CD3_3']
        del ahdr5['CTYPE3A']
        del ahdr5['CUNIT3A']
        del ahdr5['CNAME3A']
        del ahdr5['CRVAL3A']
        del ahdr5['CRPIX3A']
        del ahdr5['CD3_3A']

    data5=np.zeros((data4.shape[0],data4.shape[1],nx0))
    if exact_area:
        area5=np.zeros((data4.shape[0],data4.shape[1],nx0))
    else:
        area5=np.zeros((data4.shape[1],nx0))
    ratio=int(dx0/dx)
    if ratio!=1:
        #warnings.filterwarnings('ignore')
        for i in range(nx0):
            tmp=data4[:,:,i*ratio:(i+1)*ratio]
            data5[:,:,i]=np.nanmean(tmp,axis=2)
            if exact_area:
                tmp=area4[:,:,i*ratio:(i+1)*ratio]
                area5[:,:,i]=np.sum(tmp,axis=2)
            else:
                tmp=area4[:,i*ratio:(i+1)*ratio]
                area5[:,i]=np.sum(tmp,axis=1)
        #warnings.resetwarnings()

    
    # Compress
    if compress==True:
        if nx0==1:
            tmp=hdr5.copy()
            hdr5['NAXIS']=2
            hdr5['NAXIS1']=tmp['NAXIS3']
            hdr5['NAXIS2']=tmp['NAXIS2']
            del hdr5['NAXIS3']
            hdr5['CTYPE1']=tmp['CTYPE3']
            hdr5['CTYPE2']=tmp['CTYPE2']
            hdr5['CTYPE1A']=tmp['CTYPE3A']
            hdr5['CTYPE2A']=tmp['CTYPE2A']
            del hdr5['CTYPE3']
            del hdr5['CTYPE3A']
            hdr5['CUNIT1']=tmp['CUNIT3']
            hdr5['CUNIT2']=tmp['CUNIT2']
            hdr5['CUNIT1A']=tmp['CUNIT3A']
            hdr5['CUNIT2A']=tmp['CUNIT2A']
            del hdr5['CUNIT3']
            del hdr5['CUNIT3A']
            hdr5['CNAME1']=tmp['CNAME3']
            hdr5['CNAME2']=tmp['CNAME2']
            hdr5['CNAME1A']=tmp['CNAME3A']
            hdr5['CNAME2A']=tmp['CNAME2A']
            del hdr5['CNAME3']
            del hdr5['CNAME3A']
            hdr5['CRVAL1']=tmp['CRVAL3']
            hdr5['CRVAL2']=tmp['CRVAL2']
            hdr5['CRVAL1A']=tmp['CRVAL3A']
            hdr5['CRVAL2A']=tmp['CRVAL2A']
            del hdr5['CRVAL3']
            del hdr5['CRVAL3A']
            hdr5['CRPIX1']=tmp['CRPIX3']
            hdr5['CRPIX2']=tmp['CRPIX2']
            hdr5['CRPIX1A']=tmp['CRPIX3A']
            hdr5['CRPIX2A']=tmp['CRPIX2A']
            del hdr5['CRPIX3']
            del hdr5['CRPIX3A']
            hdr5['CD1_1']=tmp['CD3_3']
            hdr5['CD2_2']=tmp['CD2_2']
            hdr5['CD1_1A']=tmp['CD3_3A']
            hdr5['CD2_2A']=tmp['CD2_2A']
            del hdr5['CD3_3']
            del hdr5['CD3_3A']
            data5=np.transpose(np.squeeze(data5,axis=2))
            
            if exact_area:
                ahdr5=hdr5.copy()
                area5=np.transpose(np.squeeze(area5,axis=2))
            else:
                tmp=ahdr5.copy()
                ahdr5['NAXIS']=1
                ahdr5['NAXIS1']=tmp['NAXIS2']
                del ahdr5['NAXIS2']
                ahdr5['CTYPE1']=tmp['CTYPE2']
                ahdr5['CTYPE1A']=tmp['CTYPE2A']
                del ahdr5['CTYPE2']
                del ahdr5['CTYPE2A']
                ahdr5['CUNIT1']=tmp['CUNIT2']
                ahdr5['CUNIT1A']=tmp['CUNIT2A']
                del ahdr5['CUNIT2']
                del ahdr5['CUNIT2A']
                ahdr5['CNAME1']=tmp['CNAME2']
                ahdr5['CNAME1A']=tmp['CNAME2A']
                del ahdr5['CNAME2']
                del ahdr5['CNAME2A']
                ahdr5['CRVAL1']=tmp['CRVAL2']
                ahdr5['CRVAL1A']=tmp['CRVAL2A']
                del ahdr5['CRVAL2']
                del ahdr5['CRVAL2A']
                ahdr5['CRPIX1']=tmp['CRPIX2']
                ahdr5['CRPIX1A']=tmp['CRPIX2A']
                del ahdr5['CRPIX2']
                del ahdr5['CRPIX2A']
                ahdr5['CD1_1']=tmp['CD2_2']
                ahdr5['CD1_1A']=tmp['CD2_2A']
                del ahdr5['CD2_2']
                del ahdr5['CD2_2A']
                area5=np.transpose(np.squeeze(area5))

        elif ny==1:
            tmp=hdr5.copy()
            hdr5['NAXIS']=2
            hdr5['NAXIS1']=tmp['NAXIS3']
            hdr5['NAXIS2']=tmp['NAXIS1']
            del hdr5['NAXIS3']
            hdr5['CTYPE1']=tmp['CTYPE3']
            hdr5['CTYPE2']=tmp['CTYPE1']
            hdr5['CTYPE1A']=tmp['CTYPE3A']
            hdr5['CTYPE2A']=tmp['CTYPE1A']
            del hdr5['CTYPE3']
            del hdr5['CTYPE3A']
            hdr5['CUNIT1']=tmp['CUNIT3']
            hdr5['CUNIT2']=tmp['CUNIT1']
            hdr5['CUNIT1A']=tmp['CUNIT3A']
            hdr5['CUNIT2A']=tmp['CUNIT1A']
            del hdr5['CUNIT3']
            del hdr5['CUNIT3A']
            hdr5['CNAME1']=tmp['CNAME3']
            hdr5['CNAME2']=tmp['CNAME1']
            hdr5['CNAME1A']=tmp['CNAME3A']
            hdr5['CNAME2A']=tmp['CNAME1A']
            del hdr5['CNAME3']
            del hdr5['CNAME3A']
            hdr5['CRVAL1']=tmp['CRVAL3']
            hdr5['CRVAL2']=tmp['CRVAL1']
            hdr5['CRVAL1A']=tmp['CRVAL3A']
            hdr5['CRVAL2A']=tmp['CRVAL1A']
            del hdr5['CRVAL3']
            del hdr5['CRVAL3A']
            hdr5['CRPIX1']=tmp['CRPIX3']
            hdr5['CRPIX2']=tmp['CRPIX1']
            hdr5['CRPIX1A']=tmp['CRPIX3A']
            hdr5['CRPIX2A']=tmp['CRPIX1A']
            del hdr5['CRPIX3']
            del hdr5['CRPIX3A']
            hdr5['CD1_1']=tmp['CD3_3']
            hdr5['CD2_2']=tmp['CD1_1']
            hdr5['CD1_1A']=tmp['CD3_3A']
            hdr5['CD2_2A']=tmp['CD1_1A']
            del hdr5['CD3_3']
            del hdr5['CD3_3A']
            data5=np.transpose(np.squeeze(data5,axis=1))

            
    hdu5=fits.PrimaryHDU(data5,header=hdr5)
    ahdu5=fits.PrimaryHDU(area5,header=ahdr5)

            

    if writefn!='':
        hdu5.writeto(writefn,overwrite=True)
        ahdu5.writeto(writefn.replace('.fits','_area.fits'),overwrite=True)

    if clean==True:
        os.system('rm -f kcwi_tools/cart2cyli*')
        
    return (hdu5,ahdu5)