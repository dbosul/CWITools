"""Tools for extended data reduction."""

from cwitools import coordinates,  modeling, utils, synthesis
from astropy import units as u
from astropy.io import fits
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_scales
import astropy.stats
import astropy.coordinates
from PyAstronomy import pyasl
import reproject
from scipy.interpolate import interp1d
from scipy import ndimage
from scipy.ndimage.filters import convolve
from scipy.ndimage.measurements import center_of_mass
from scipy.signal import correlate
from scipy.stats import sigmaclip
from shapely.geometry import box, Polygon
from tqdm import tqdm

import argparse
import matplotlib
import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import numpy as np
import sys
import time
import warnings

def slice_corr(fits_in):
    """Perform slice-by-slice median correction for scattered light.


    Args:
        fits_in (HDU or HDUList): The input data cube

    Returns:
        HDU or HDUList (same type as input): The corrected data

    """

    hdu = utils.extractHDU(fits_in)
    data, header = hdu.data, hdu.header

    instrument = utils.get_instrument(hdu)
    if instrument == "PCWI":
        slice_axis = 1
    elif instrument == "KCWI":
        slice_axis = 2
    else:
        raise ValueError("Unrecognized instrument")

    slice_axis = np.nanargmin(data.shape)
    nslices = data.shape[slice_axis]

    #Run through slices
    for i in tqdm(range(nslices)):

        if slice_axis == 1:
            slice_2d = data[:, i, :]
        elif slice_axis == 2:
            slice_2d = data[:, :, i]
        else:
            raise RuntimeError("Shortest axis should be slice axis.")

        xdomain = np.arange(slice_2d.shape[1])

        #Run through wavelength layers
        for wi in range(slice_2d.shape[0]):

            xprof = slice_2d[wi]
            clipped, lower, upper = sigmaclip(xprof, low=2, high=2)
            usex = (xprof >= lower) & (xprof <= upper)
            bg_model = np.median(xprof[usex])


            if slice_axis == 1:
                fits_in[0].data[wi, i, :] -= bg_model
            else:
                fits_in[0].data[wi, :, i] -= bg_model

    return fits_in


def estimate_variance(inputfits, window=50, sclip=None, wmasks=[], fmin=0.9):
    """Estimates the 3D variance cube of an input cube.

    Args:
        inputfits (astropy.io.fits.HDUList): FITS object to estimate variance of.
        window (int): Wavelength window (Angstrom) to use for local 2D variance estimation.
        wmasks (list): List of wavelength tuples to exclude when estimating variance.
        sclip (float): Sigmaclip threshold to apply when comparing layer-by-layer noise.
        fMin (float): The minimum rescaling factor (Default 0.9)

    Returns:
        NumPy ndarray: Estimated variance cube

    """

    cube = inputfits[0].data.copy()
    varcube = np.zeros_like(cube)
    z, y, x = cube.shape
    Z = np.arange(z)
    wav_axis = coordinates.get_wav_axis(inputfits[0].header)
    cd3_3 = inputfits[0].header["CD3_3"]

    #Create wavelength masked based on input
    zmask = np.ones_like(wav_axis, dtype=bool)
    for (w0, w1) in wmasks:
        zmask[(wav_axis > w0) & (wav_axis < w1)] = 0
    nzmax = np.count_nonzero(zmask)

    #Loop over wavelength first to minimize repetition of wl-mask calculation
    for j, wav_j in enumerate(wav_axis):

        #Get initial width of white-light bandpass in px
        width_px = window / cd3_3

        #Create initial white-light mask, centered on j with above width
        vmask = zmask & (np.abs(Z - j) <= width_px / 2)

        #Grow until minimum number of valid wavelength layers included
        while np.count_nonzero(vmask) < min(nzmax, window / cd3_3):
            width_px += 2
            vmask = zmask & (np.abs(Z - j) <= width_px / 2)

        varcube[j] = np.var(cube[vmask], axis=0)

    #Adjust first estimate by rescaling, if set to do so
    varcube = rescale_var(varcube, cube, fmin=fmin, sclip=sclip)

    rescaleF = np.var(cube) / np.mean(varcube)
    varcube *= rescaleF

    return varcube

def rescale_var(varcube, datacube, fmin=0.9, sclip=4):
    """Rescale a variance cube layer-by-layer to reflect the noise of a data cube.

    Args:
        varcube (NumPy.ndarray): Variance cube to rescale.
        datacube (NumPy.ndarray): Data cube corresponding to variance cube.
        fmin (float): Minimum rescaling factor (Default=0.9)

    Returns:

        NumPy ndarray: Rescaled variance cube

    Examples:

        >>> from astropy.io import fits
        >>> from cwitools.variance import rescale_variance
        >>> data = fits.open("data.fits")
        >>> var  = fits.getdata("variance.fits")
        >>> var_rescaled = rescale_variance(var, data)

    """
    for wi in range(varcube.shape[0]):

        useXY = varcube[wi] > 0

        layer_data = datacube[wi][useXY]
        layer_var = varcube[wi][useXY]

        # if sclip != None:
        #     layer_data = sigmaclip(layer_data, low=sclip, high=sclip).clipped
        #     layer_var = sigmaclip(layer_var, low=sclip, high=sclip).clipped

        rsFactor = np.var(layer_data) / np.mean(layer_var)
        rsFactor = max(rsFactor, fmin)

        varcube[wi] *= rsFactor

    return varcube

def xcor_crpix3(fits_list, xmargin=2, ymargin=2):
    """Get relative offsets in wavelength axis by cross-correlating sky spectra.

    Args:
        fits_list (Astropy.io.fits.HDUList list): List of sky cube FITS objects.
        xmargin (int): Margin to use along FITS axis 1 when summing spatially to
            create spectra. e.g. xmargin = 2 - exclude the edge 2 pixels left
            and right from contributing to the spectrum.
        ymargin (int): Margin to use along fits axis 2 when creating spevtrum.

    Returns:
        crpix3_corr (list): List of corrected CRPIX3 values.

    """
    #Extract wavelength axes and normalized sky spectra from each fits
    N = len(fits_list)
    wavs, spcs, crval3s, crpix3s = [], [], [], []
    for i, sky_fits in enumerate(fits_list):

        sky_data, sky_hdr = sky_fits[0].data, sky_fits[0].header
        sky_data = np.nan_to_num(sky_data, nan=0, posinf=0, neginf=0)

        wav = coordinates.get_wav_axis(sky_hdr)

        sky = np.sum(sky_data[:, ymargin:-ymargin, xmargin:-xmargin], axis=(1, 2))
        sky /= np.max(sky)

        spcs.append(sky)
        wavs.append(wav)
        crval3s.append(sky_hdr["CRVAL3"])
        crpix3s.append(sky_hdr["CRPIX3"])

    #Create common wavelength axis to interpolate sky spectra onto
    w0, w1 = np.min(wavs), np.max(wavs)
    dw_min = np.min([x[1] - x[0] for x in wavs])
    Nw = int((w1 - w0) / dw_min) + 1
    wav_common = np.linspace(w0, w1, Nw)

    #Interpolate (linearly) spectra onto common wavelength axis
    spc_interps = [interp1d(wavs[i], spcs[i])(wav_common) for i in range(N)]

    #Cross-correlate interpolated spectra to look for shifts between them
    corrs = []
    for i, spc_int in enumerate(spc_interps):
        corr_ij = correlate(spc_interps[0], spc_int, mode='full')
        corrs.append(np.nanargmax(corr_ij))

    #Subtract first self-correlation (reference point)
    corrs = corrs[0] -  np.array(corrs)

    #Create new
    crpix3s_corr = [crpix3s[i] + c for i, c in enumerate(corrs)]

    #Return corrections to CRPIX3 values
    return crpix3s

def xcor_2d(hdu0_in, hdu1_in, preshift=[0,0], maxstep=None, box=None, upscale=1, conv_filter=2.,
            background_subtraction=False, background_level=None, reset_center=False,
            method='interp-bicubic', output_flag=False, plot=0):
    """Perform 2D cross correlation to image HDUs and returns the relative shifts.
    This function is the base of xcor_cr12() for frame alignment. 
    
    Args:
        hdu0_in (astropy HDU / HDUList): Input HDU/HDUList with 2D data for reference.
        
        hdu1_in (astropy HDU / HDUList): Input HDU/HDUList with 2D data to be shifted.
        
        preshift (float tuple): If any shift need to be applied prior to xcor.
            TODO: This need to be updated to CRVAL/CRPIX style to be user-friendly.
        
        maxstep (int tupe): Maximum pixel search range in X and Y directions. 
            Default is 1/4 of the image size.
        
        box (int tuple): Specify a certain region in [X0, Y0, X1, Y1] of HDU0 to be 
            cross-correlated.
            Default is the whole image.
        
        upscale (int): Factor for increased sampling. 
        
        conv_filter (float): Size of the convolution filter when searching for the local
            maximum in the xcor map.
        
        background_subtraction (bool): Apply background subtraction to the image?
            If "background_level" is not specified, it uses median as the background.
        
        background_level (float tuple): Background value of the two images. Pixels below 
            these will be ignored. 
            
        reset_center (bool): Ignore the WCS information in HDU1 and force its center to be
            the same as HDU0.
            
        method (str): Sampling method for sub-pixel interpolations. Supported values:
            "interp-nearest", "interp-bilinear", "inter-bicubic" (Default), "exact". 
            
        output_flag (bool): If set return [xshift, yshift, flag] even if the program failed
            to locate a local maximum (flag = 0). Otherwise, return [xshift, yshift] only if
            a  local maximum if found. 
        
        plot (int): Make plots? 
            0 - No plot. 
            1 - Only the xcor map.
            2 - All diagnostic plots.
            
    Return:
        x_final (float): Amount of shift in X that need to be added to CRPIX1.
        y_final (float): Amount of shift in Y that need to be added to CRPIX2. 
        flag (bool) (Only return if output_flag == True.): 
            0 - Failed to locate a local maximum, thus x_final and y_final are unreliable.
            1 - Success.
            
    """

    if 'interp' in method:
        _,interp_method=method.split('-')
        def tmpfunc(hdu1,header):
            return reproject.reproject_interp(hdu1,header,order=interp_method)
        reproject_func=tmpfunc
    elif 'exact' in method:
        reproject_func=rerpoject.reproject_exact
    else:
        raise ValueError('Interpolation method not recognized.')

    upscale=int(upscale)

    # Properties
    hdu1_old=hdu1_in
    hdu0=hdu0_in.copy()
    hdu1=hdu1_in.copy()
    hdu0.data=np.nan_to_num(hdu0.data,nan=0,posinf=0,neginf=0)
    hdu1.data=np.nan_to_num(hdu1.data,nan=0,posinf=0,neginf=0)
    sz0=hdu0.shape
    sz1=hdu1.shape
    wcs0_old=WCS(hdu0.header)
    wcs1_old=WCS(hdu1.header)

    old_crpix1=[hdu1.header['CRPIX1'],hdu1.header['CRPIX2']]

    # defaults
    if maxstep is None:
        maxstep=[sz1[1]/4.,sz1[0]/4.]
    maxstep=[int(np.round(i)) for i in maxstep]

    if box is None:
        box=[0,0,sz0[1],sz0[0]]
        
    if reset_center:
        ad_center0=wcs0_old.all_pix2world(sz0[1]/2+0.5,sz0[0]/2+0.5,0)
        ad_center0=[float(i) for i in ad_center0]

        xy_center0to1=wcs1_old.all_world2pix(*ad_center0,0)
        xy_center0to1=[float(i) for i in xy_center0to1]

        dcenter=[(sz1[1]/2+0.5)-xy_center0to1[0],(sz1[0]/2+0.5)-xy_center0to1[1]]
        hdu1.header['CRPIX1']+=dcenter[0]
        hdu1.header['CRPIX2']+=dcenter[1]

    # preshifts
    hdu1.header['CRPIX1']+=preshift[0]
    hdu1.header['CRPIX2']+=preshift[1]
    
    wcs0=WCS(hdu0.header)
    wcs1=WCS(hdu1.header)

    # upscale
    def hdu_upscale(hdu,upscale,header_only=False):
        hdu_up=hdu.copy()
        if upscale!=1:
            hdr_up=hdu_up.header
            hdr_up['NAXIS1']=hdr_up['NAXIS1']*upscale
            hdr_up['NAXIS2']=hdr_up['NAXIS2']*upscale
            hdr_up['CRPIX1']=(hdr_up['CRPIX1']-0.5)*upscale+0.5
            hdr_up['CRPIX2']=(hdr_up['CRPIX2']-0.5)*upscale+0.5
            hdr_up['CD1_1']=hdr_up['CD1_1']/upscale
            hdr_up['CD2_1']=hdr_up['CD2_1']/upscale
            hdr_up['CD1_2']=hdr_up['CD1_2']/upscale
            hdr_up['CD2_2']=hdr_up['CD2_2']/upscale
            if not header_only:
                hdu_up.data,coverage=reproject_func(hdu,hdr_up)

        return hdu_up

    hdu0=hdu_upscale(hdu0,upscale)
    hdu1=hdu_upscale(hdu1,upscale)


    # project 1 to 0
    img1,cov1=reproject_func(hdu1,hdu0.header)

    img0=np.nan_to_num(hdu0.data,nan=0,posinf=0,neginf=0)
    img1=np.nan_to_num(img1,nan=0,posinf=0,neginf=0)
    img1_expand=np.zeros((sz0[0]*3*upscale,sz0[1]*3*upscale))
    img1_expand[sz0[0]*upscale:sz0[0]*2*upscale,sz0[1]*upscale:sz0[1]*2*upscale]=img1

    # +/- maxstep pix
    xcor_size=((np.array(maxstep)-1)*upscale+1)+int(np.ceil(conv_filter))
    xx=np.linspace(-xcor_size[0],xcor_size[0],2*xcor_size[0]+1,dtype=int)
    yy=np.linspace(-xcor_size[1],xcor_size[1],2*xcor_size[1]+1,dtype=int)
    dy,dx=np.meshgrid(yy,xx)

    xcor=np.zeros(dx.shape)
    for ii in range(xcor.shape[0]):
        for jj in range(xcor.shape[1]):
            cut0=img0[box[1]*upscale:box[3]*upscale,box[0]*upscale:box[2]*upscale]
            cut1=img1_expand[box[1]*upscale-dy[ii,jj]+sz0[0]*upscale:box[3]*upscale-dy[ii,jj]+sz0[0]*upscale,
                             box[0]*upscale-dx[ii,jj]+sz0[1]*upscale:box[2]*upscale-dx[ii,jj]+sz0[1]*upscale]
            if background_subtraction:
                if background_level is None:
                    back_val0=np.median(cut0[cut0!=0])
                    back_val1=np.median(cut1[cut1!=0])
                else:
                    back_val0=float(background_level[0])
                    back_val1=float(background_level[1])
                cut0=cut0-back_val0
                cut1=cut1-back_val1
            else:
                if not background_level is None:
                    cut0[cut0<background_level[0]]=0
                    cut1[cut1<background_level[1]]=0

            cut0[cut0<0]=0
            cut1[cut1<0]=0
            mult=cut0*cut1
            if np.sum(mult!=0)>0:
                xcor[ii,jj]=np.sum(mult)/np.sum(mult!=0)
        
                
    # local maxima
    max_conv=ndimage.filters.maximum_filter(xcor,2*conv_filter+1)
    maxima=(xcor==max_conv)
    labeled, num_objects=ndimage.label(maxima)
    slices=ndimage.find_objects(labeled)
    xindex,yindex=[],[]
    for dx,dy in slices:
        x_center=(dx.start+dx.stop-1)/2
        xindex.append(x_center)
        y_center=(dy.start+dy.stop-1)/2
        yindex.append(y_center)
    xindex=np.array(xindex).astype(int)
    yindex=np.array(yindex).astype(int)
    # remove boundary effect
    index=((xindex>=conv_filter) & (xindex<2*xcor_size[0]-conv_filter) &
            (yindex>=conv_filter) & (yindex<2*xcor_size[1]-conv_filter))
    xindex=xindex[index]
    yindex=yindex[index]
    # closest one
    if len(xindex)==0:
        # Error handling
        if output_flag==True:
            return 0.,0.,False
        else:
            # perhaps we can use the global maximum here, but it is also garbage...
            raise ValueError('Unable to find local maximum in the XCOR map.')

    max=np.max(max_conv[xindex,yindex])
    med=np.median(xcor)
    index=np.where(max_conv[xindex,yindex] > 0.3*(max-med)+med)
    xindex=xindex[index]
    yindex=yindex[index]
    if len(xindex)==0:
        # Error handling
        if output_flag==True:
            return 0.,0.,False
        else:
            # perhaps we can use the global maximum here, but it is also garbage...
            raise ValueError('Unable to find local maximum in the XCOR map.')
    r=(xx[xindex]**2+yy[yindex]**2)
    index=r.argmin()
    xshift=xx[xindex[index]]/upscale
    yshift=yy[yindex[index]]/upscale

    hdu1=hdu_upscale(hdu1,1/upscale,header_only=True)
    hdu0=hdu_upscale(hdu0,1/upscale,header_only=True)
    
    tmp=wcs0.all_pix2world(hdu0.header['CRPIX1']+xshift,hdu0.header['CRPIX2']+yshift,1)
    ashift=float(tmp[0])-hdu0.header['CRVAL1']
    dshift=float(tmp[1])-hdu0.header['CRVAL2']
    tmp=wcs1.all_world2pix(hdu1.header['CRVAL1']-ashift,hdu1.header['CRVAL2']-dshift,1)
    x_final=tmp[0]-old_crpix1[0]
    y_final=tmp[1]-old_crpix1[1]

    plot=int(plot)
    if plot!=0:
        if plot==1:
            fig,axes=plt.subplots(figsize=(6,6))
        elif plot==2:
            fig,axes=plt.subplots(3,2,figsize=(8,12))
        else:
            raise ValueError('Allowed values for "plot": 0, 1, 2.')

        # xcor map
        if plot==2:
            ax=axes[0,0]
        elif plot==1:
            ax=axes
        xplot=(np.append(xx,xx[1]-xx[0]+xx[-1])-0.5)/upscale
        yplot=(np.append(yy,yy[1]-yy[0]+yy[-1])-0.5)/upscale
        colormesh=ax.pcolormesh(xplot,yplot,xcor.T)
        xlim=ax.get_xlim()
        ylim=ax.get_ylim()
        ax.plot([xplot.min(),xplot.max()],[0,0],'w--')
        ax.plot([0,0],[yplot.min(),yplot.max()],'w--')
        ax.plot(xshift,yshift,'+',color='r',markersize=20)
        ax.set_xlabel('dx')
        ax.set_ylabel('dy')
        ax.set_title('XCOR_MAP')
        fig.colorbar(colormesh,ax=ax)

        if plot==2:
            fig.delaxes(axes[0,1])

            # adu0
            cut0_plot=img0[box[1]*upscale:box[3]*upscale,box[0]*upscale:box[2]*upscale]
            ax=axes[1,0]
            imshow=ax.imshow(cut0_plot,origin='bottom')
            ax.set_xlabel('x')
            ax.set_ylabel('y')
            ax.set_title('Ref img')
            fig.colorbar(imshow,ax=ax)

            # adu1
            cut1_plot=img1_expand[box[1]*upscale+sz0[0]*upscale:box[3]*upscale+sz0[0]*upscale,
                                 box[0]*upscale+sz0[1]*upscale:box[2]*upscale+sz0[1]*upscale]
            ax=axes[1,1]
            imshow=ax.imshow(cut1_plot,origin='bottom')
            ax.set_xlabel('x')
            ax.set_ylabel('y')
            ax.set_title('Original img')
            fig.colorbar(imshow,ax=ax)


            # sub1
            ax=axes[2,0]
            imshow=ax.imshow(cut1_plot-cut0_plot,origin='bottom')
            ax.set_xlabel('x')
            ax.set_ylabel('y')
            ax.set_title('Original sub')
            fig.colorbar(imshow,ax=ax)


            # sub2
            cut1_best=img1_expand[(box[1]+sz0[0]-int(yshift))*upscale:(box[3]+sz0[0]-int(yshift))*upscale,
                                  (box[0]+sz0[1]-int(xshift))*upscale:(box[2]+sz0[1]-int(xshift))*upscale]
            ax=axes[2,1]
            imshow=ax.imshow(cut1_best-cut0_plot,origin='bottom')
            ax.set_xlabel('x')
            ax.set_ylabel('y')
            ax.set_title('Best sub')
            fig.colorbar(imshow,ax=ax)


        fig.tight_layout()
        plt.show()

    if output_flag==True:
        return x_final,y_final,True
    else:
        return x_final,y_final

def xcor_cr12(fits_in, fits_ref, wmask=[], preshift=[0,0], maxstep=None, box=None, 
              pixscale=None, orientation=None, dimension=None, 
              upscale=10., conv_filter=2.,
              background_subtraction=False, background_level=None, 
              reset_center=False, method='interp-bicubic', plot=1):
    """Using cross-correlation to measure the true CRPIX1/2 and CRVAL1/2 keywords in 3D cubes.
    This function is a wrapper of xcor_2d() to optimize the reduction process.
    
    Args:
        fits_in (astropy HDU / HDUList): Input HDU/HDUList with 3D data to be shifted.
        
        fits_ref (astropy HDU / HDUList): Input HDU/HDUList with 3D data as reference.
        
        wmask (float tuple): Wavelength bins in which the cube is collapsed into a 
            whitelight image.
        
        preshift (float tuple): If any shift need to be applied prior to xcor.
            TODO: This need to be updated to CRVAL/CRPIX style to be user-friendly.
        
        maxstep (int tupe): Maximum pixel search range in X and Y directions. 
            Default is 1/4 of the image size.
        
        box (int tuple): Specify a certain region in [X0, Y0, X1, Y1] of HDU0 to be 
            cross-correlated.
            Default is the whole image.
            
        pixscale (float tuple): Size of pixels in X and Y in arcsec of the reference grid.
            Default is the smallest size between X and Y of "fits_ref".
            
        orienation (float): Position angle of Y axis.
            Default: The same as "fits_ref".
            
        Dimension (float tuple): Size of the reference grid. 
            Default: Just enough to contain the whole "fits_ref". 
        
        upscale (int): Factor for increased sampling during the 2nd iteration. This determines
            the output precision.
        
        conv_filter (float): Size of the convolution filter when searching for the local
            maximum in the xcor map.
        
        background_subtraction (bool): Apply background subtraction to the image?
            If "background_level" is not specified, it uses median as the background.
        
        background_level (float tuple): Background value of the two images. Pixels below 
            these will be ignored. 
            
        reset_center (bool): Ignore the WCS information in HDU1 and force its center to be
            the same as HDU0.
            
        method (str): Sampling method for sub-pixel interpolations. Supported values:
            "interp-nearest", "interp-bilinear", "inter-bicubic" (Default), "exact". 
            
        plot (int): Make plots? 
            0 - No plot. 
            1 - Only the xcor map.
            2 - All diagnostic plots.
            
    Return:
        crpix1 (float): True value of CRPIX1.
        crpix2 (float): True value of CRPIX2. 
        crval1 (float): True value of CRVAL1
        crval2 (float): True value of CRVAL2

    """
    
    hdu=utils.extractHDU(fits_in)
    hdu_ref=utils.extractHDU(fits_ref)

    # whitelight images
    hdu_img,_=synthesis.whitelight(hdu,wmask=wmask,mask_sky=True)
    hdu_img_ref,_=synthesis.whitelight(hdu_ref,wmask=wmask,mask_sky=True)

    # post projecttion pixel size
    px=np.sqrt(hdu_ref.header['CD1_1']**2+hdu_ref.header['CD2_1']**2)*3600.
    py=np.sqrt(hdu_ref.header['CD1_2']**2+hdu_ref.header['CD2_2']**2)*3600.
    if pixscale is None:
        pixscale=[np.min(px,py),np.min(px,py)]
        pixscale_x=pixscale[0]
        pixscale_y=pixscale[1]

    # post projection image size
    if dimension is None:
        d_x=int(np.round(px*hdu_ref.shape[2]/pixscale_x))
        d_y=int(np.round(py*hdu_ref.shape[1]/pixscale_y))
        dimension=[d_x,d_y]

    if plot==0:
        oldbackend=matplotlib.get_backend()
        matplotlib.use('Agg')

    # construct WCS for the reference HDU in uniform grid
    hdrtmp=hdu_img_ref.header.copy()
    wcstmp=WCS(hdrtmp).copy()
    center=wcstmp.wcs_pix2world((wcstmp.pixel_shape[0]-1)/2.,
                                (wcstmp.pixel_shape[1]-1)/2.,0,ra_dec_order=True)

    hdr0=hdrtmp.copy()
    hdr0['NAXIS1']=dimension[0]
    hdr0['NAXIS2']=dimension[1]
    hdr0['CRPIX1']=(dimension[0]+1)/2.
    hdr0['CRPIX2']=(dimension[1]+1)/2.
    hdr0['CRVAL1']=float(center[0])
    hdr0['CRVAL2']=float(center[1])
    old_cd11=hdr0['CD1_1']
    old_cd12=hdr0['CD1_2']
    old_cd21=hdr0['CD2_1']
    old_cd22=hdr0['CD2_2']
    hdr0['CD1_1']=-pixscale_x/3600
    hdr0['CD2_2']=pixscale_y/3600
    hdr0['CD1_2']=0.
    hdr0['CD2_1']=0.

    # orientation
    if orientation==None:
        orientation=np.ra2deg(np.arctan(old_cd21/(-old_cd11)))
    hdr0['CD1_1']=-pixscale_x/3600*np.cos(np.deg2rad(orientation))
    hdr0['CD2_1']=pixscale_x/3600*np.sin(np.deg2rad(orientation))
    hdr0['CD1_2']=pixscale_y/3600*np.sin(np.deg2rad(orientation))
    hdr0['CD2_2']=pixscale_y/3600*np.cos(np.deg2rad(orientation))

    # project the refrence hdu to this new standard grid
    if 'interp' in method:
        interpmethod=method.split('-')[1]
        img_ref0,_=reproject.reproject_interp(hdu_img_ref,hdr0,order=interpmethod)
        hdu_img_ref0=fits.PrimaryHDU(img_ref0,hdr0)
    elif 'exact' in method:
        img_ref0,_=reproject.reproject_exact(hdu_img_ref,hdr0)
        hdu_img_ref0=fits.PrimaryHDU(img_ref0,hdr0)
    else:
        raise ValueError('Interpolation method not recognized.')
            
    # First iteration
    dx,dy,flag=xcor_2d(hdu_img_ref0,hdu_img,preshift=preshift,
                       maxstep=maxstep,box=box,upscale=1,conv_filter=conv_filter,
                       background_subtraction=background_subtraction,
                       background_level=background_level,
                       reset_center=reset_center,method=method,output_flag=True,plot=plot)
    if flag==False:
        if reset_center==False:
            utils.output('\tFirst attempt failed. Trying to recenter\n')
            dx,dy=xcor_2d(hdu_img_ref0,hdu_img,preshift=preshift,
                   maxstep=maxstep,box=box,upscale=1,conv_filter=conv_filter,
                   background_subtraction=background_subtraction,
                   background_level=background_level,
                   reset_center=True,method=method,output_flag=True,plot=plot)
        else:
            raise ValueError('Unable to find local maximum in the XCOR map.')
    
    utils.output('\tFirst iteration:\n')
    utils.output("\t\tdx = %.2f, dy = %.2f\n" % (dx,dy))
        
           
    # iteration 2: with upscale
    dx2,dy2=xcor_2d(hdu_img_ref0,hdu_img,preshift=[dx,dy],
                       maxstep=[2,2],box=box,upscale=upscale,conv_filter=conv_filter,
                       background_subtraction=background_subtraction,
                       background_level=background_level,
                       method=method,plot=plot)
    
    utils.output('\tSecond iteration:\n')
    utils.output("\t\tdx = %.2f, dy = %.2f\n" % (dx2,dy2))
        
    # get returning dataset
    crpix1=hdu_img.header['CRPIX1']+dx2
    crpix2=hdu_img.header['CRPIX2']+dy2
    crval1=hdu_img.header['CRVAL1']
    crval2=hdu_img.header['CRVAL2']

    return crpix1,crpix2,crval1,crval2

def fit_crpix12(fits_in, crval1, crval2, box_size=10, plot=False, iters=3, std_max=4):
    """Measure the position of a known source to get crpix1 and crpix2.

    Args:
        fits_in (Astropy.io.fits.HDUList): The input data cube as a fits object
        crval1 (float): The RA/CRVAL1 of the known source
        crval2 (float): The DEC/CRVAL2 of the known source
        crpix12_guess (int tuple): The estimated x,y location of the source.
            If none provided, the existing WCS will be used to estimate x,y.
        box_size (float): The size of the box (in arcsec) to use for measuring.

    Returns:
        cpix1 (float): The axis 1 centroid of the source
        cpix2 (float): The axis 2 centroid of the source

    """

    # Convention here is that cube dimensions are (w, y, x)
    # For KCWI - x is the across-slice axis, for PCWI it is y

    #Load input
    cube = fits_in[0].data.copy()
    header3d = fits_in[0].header

    #Create 2D WCS and get pixel sizes in arcseconds
    header2d = coordinates.get_header2d(header3d)
    wcs2d = WCS(header2d)
    pixel_scales = proj_plane_pixel_scales(wcs2d)
    y_scale = (pixel_scales[1] * u.deg).to(u.arcsec).value
    x_scale = (pixel_scales[0] * u.deg).to(u.arcsec).value

    #Get initial estimate of source position
    crpix1, crpix2 = wcs2d.all_world2pix(crval1, crval2, 0)

    #Limit cube to good wavelength range and clean cube
    wavgood0, wavgood1 = header3d["WAVGOOD0"], header3d["WAVGOOD1"]
    wav_axis = coordinates.get_wav_axis(header3d)
    use_wav = (wav_axis > wavgood0) & (wav_axis < wavgood1)
    cube[~use_wav] = 0
    cube = np.nan_to_num(cube, nan=0, posinf=0, neginf=0)

    #Create WL image
    wl_img = np.sum(cube, axis=0)
    wl_img -= np.median(wl_img)

    #Extract box and measure centroid
    box_size_x = box_size / x_scale
    box_size_y = box_size / y_scale

    #Get bounds of box - limited by image bounds.
    x0 = max(0, int(crpix1 - box_size_x / 2))
    x1 = min(cube.shape[2] - 1, int(crpix1 + box_size_x / 2 + 1))

    y0 = max(0, int(crpix2 - box_size_y / 2))
    y1 = min(cube.shape[1] - 1, int(crpix2 + box_size_y / 2 + 1))

    #Create data structures for fitting
    x_domain = np.arange(x0, x1)
    y_domain = np.arange(y0, y1)

    x_prof = np.sum(wl_img[y0:y1, x0:x1], axis=0)
    y_prof = np.sum(wl_img[y0:y1, x0:x1], axis=1)

    x_prof /= np.max(x_prof)
    y_prof /= np.max(y_prof)

    #Determine bounds for gaussian profile fit
    x_bounds = [
        (0, 10),
        (x0, x1),
        (0, std_max / x_scale)
    ]
    y_bounds = [
        (0, 10),
        (y0, y1),
        (0, std_max / y_scale)
    ]

    #Run differential evolution fit on each profile
    x_fit = modeling.fit_model1d(modeling.gauss1d, x_bounds, x_domain, x_prof)
    y_fit = modeling.fit_model1d(modeling.gauss1d, y_bounds, y_domain, y_prof)

    x_center, y_center = x_fit.x[1], y_fit.x[1]

    #Fit Gaussian to each profile
    if plot:

        x_prof_model = modeling.gauss1d(x_fit.x, x_domain)
        y_prof_model = modeling.gauss1d(y_fit.x, y_domain)

        fig, axes = plt.subplots(2, 2, figsize=(8,8))
        TL, TR = axes[0, :]
        BL, BR = axes[1, :]
        TL.set_title("Full Image")
        TL.pcolor(wl_img, vmin=0, vmax=wl_img.max())
        TL.plot( [x0, x0], [y0, y1], 'w-')
        TL.plot( [x0, x1], [y1, y1], 'w-')
        TL.plot( [x1, x1], [y1, y0], 'w-')
        TL.plot( [x1, x0], [y0, y0], 'w-')
        TL.plot( x_center + 0.5, y_center + 0.5, 'rx')
        TL.set_aspect(y_scale/x_scale)

        TR.set_title("%.1f x %.1f Arcsec Box" % (box_size, box_size))
        TR.pcolor(wl_img[y0:y1, x0:x1], vmin=0, vmax=wl_img.max())
        TR.plot( x_center + 0.5 - x0, y_center + 0.5 - y0, 'rx')
        TR.set_aspect(y_scale/x_scale)

        BL.set_title("X Profile Fit")
        BL.plot(x_domain, x_prof, 'k.-', label="Data")
        BL.plot(x_domain, x_prof_model, 'r--', label="Model")
        BL.plot( [x_center]*2, [0,1], 'r--')
        BL.legend()

        BR.set_title("Y Profile Fit")
        BR.plot(y_domain, y_prof, 'k.-', label="Data")
        BR.plot(y_domain, y_prof_model, 'r--', label="Model")
        BR.plot( [y_center]*2, [0,1], 'r--')
        BR.legend()

        for ax in fig.axes:
            ax.set_xticks([])
            ax.set_yticks([])
        fig.tight_layout()
        fig.show()
        plt.waitforbuttonpress()
        plt.close()

    #Return
    return x_center + 1, y_center + 1

def rebin(inputfits, xybin=1, zbin=1, vardata=False):
    """Re-bin a data cube along the spatial (x,y) and wavelength (z) axes.

    Args:
        inputfits (astropy FITS object): Input FITS to be rebinned.
        xybin (int): Integer binning factor for x,y axes. (Def: 1)
        zbin (int): Integer binning factor for z axis. (Def: 1)
        vardata (bool): Set to TRUE if rebinning variance data. (Def: True)
        fileExt (str): File extension for output (Def: .binned.fits)

    Returns:
        astropy.io.fits.HDUList: The re-binned cube with updated WCS/Header.

    Examples:

        Bin a cube by 4 pixels along the wavelength (z) axis:

        >>> from astropy.io import fits
        >>> from cwitools import rebin
        >>> myfits = fits.open("mydata.fits")
        >>> binned_fits = rebin(myfits, zbin = 4)
        >>> binned_fits.writeto("mydata_binned.fits")


    """


    #Extract useful structures
    data = inputfits[0].data.copy()
    head = inputfits[0].header.copy()

    #Get dimensions & Wav array
    z, y, x = data.shape
    wav = coordinates.get_wav_axis(head)

    #Get new sizes
    znew = int(z // zbin)
    ynew = int(y // xybin)
    xnew = int(x // xybin)

    #Perform wavelenght-binning first, if bin provided
    if zbin > 1:

        #Get new bin size in Angstrom
        zbinSize = zbin * head["CD3_3"]

        #Create new data cube shape
        data_zbinned = np.zeros((znew, y, x))

        #Run through all input wavelength layers and add to new cube
        for zi in range(znew * zbin):
            data_zbinned[int(zi // zbin)] += data[zi]

        #Normalize so that units remain as "erg/s/cm2/A"
        if vardata: data_zbinned /= zbin**2
        else: data_zbinned /= zbin

        #Update central reference and pixel scales
        head["CD3_3"] *= zbin
        head["CRPIX3"] /= zbin

    else:

        data_zbinned = data

    #Perform spatial binning next
    if xybin > 1:

        #Get new shape
        data_xybinned = np.zeros((znew, ynew, xnew))

        #Run through spatial pixels and add
        for yi in range(ynew * xybin):
            for xi in range(xnew * xybin):
                xindex = int(xi // xybin)
                yindex = int(yi // xybin)
                data_xybinned[:, yindex, xindex] += data_zbinned[:, yi, xi]

        #
        # No normalization needed for binning spatial pixels.
        # Units remain as 'per pixel' but pixel size changes.
        #

        #Update reference pixel
        head["CRPIX1"] /= float(xybin)
        head["CRPIX2"] /= float(xybin)

        #Update pixel scales
        for key in ["CD1_1", "CD1_2", "CD2_1", "CD2_2"]: head[key] *= xybin

    else: data_xybinned = data_zbinned

    binnedFits = fits.HDUList([fits.PrimaryHDU(data_xybinned)])
    binnedFits[0].header = head

    return binnedFits

def get_crop_param(fits_in, zero_only=False, pad=0, nsig=3, plot=False):
    """Get optimized crop parameters for crop().

    Input can be ~astropy.io.fits.HDUList, ~astropy.io.fits.PrimaryHDU or
    ~astropy.io.fits.ImageHDU. If HDUList given, PrimaryHDU will be used.

    Returned objects will be of same type as input.

    Args:
        fits_in (astropy HDU / HDUList): Input HDU/HDUList with 3D data.
        zero_only (bool): Set to only crop zero-valued pixels.
        pad (int / List of three int): Additonal padding on the edges.
            Single int: The same padding is applied to all three directions.
            List of 3 int: Padding in the [x, y, z] directions.
        nsig (float): Number of sigmas in sigma-clipping.
        plot (bool): Make diagnostic plots?

    Returns:
        xcrop (int tuple): Padding indices in the x direction.
        ycrop (int tuple): Padding indices in the y direction.
        wcrop (int tuple): Padding wavelengths in the z direction.

    """

    # default param
    if np.array(pad).shape==():
        pad=np.repeat(pad,3)
    else:
        pad=np.array(pad)

    hdu = utils.extractHDU(fits_in)
    data = hdu.data.copy()
    header = hdu.header.copy()

    # instrument
    inst=utils.get_instrument(header)

    hdu_2d,_=synthesis.whitelight(hdu,mask_sky=True)
    if inst=='KCWI':
        wl=hdu_2d.data
    elif inst=='PCWI':
        wl=hdu_2d.data.T
        pad[0],pad[1]=pad[1],pad[0]
    else:
        raise ValueError('Instrument not recognized.')

    nslicer=wl.shape[1]
    npix=wl.shape[0]


    # zpad
    wav_axis = coordinates.get_wav_axis(header)

    data[np.isnan(data)] = 0
    xprof = np.max(data, axis=(0, 1))
    yprof = np.max(data, axis=(0, 2))
    zprof = np.max(data, axis=(1, 2))

    w0, w1 = header["WAVGOOD0"], header["WAVGOOD1"]
    z0, z1 = coordinates.get_indices(w0, w1, header)
    z0 += int(np.round(pad[2]))
    z1 -= int(np.round(pad[2]))
    zcrop = [z0,z1]
    wcrop = w0,w1 = [wav_axis[z0],wav_axis[z1]]

    # xpad
    xbad = xprof <= 0
    ybad = yprof <= 0

    x0 = int(np.round(xbad.tolist().index(False) + pad[0]))
    x1 = int(np.round(len(xbad) - xbad[::-1].tolist().index(False) - 1 - pad[0]))

    xcrop = [x0, x1]

    # ypad
    if zero_only:
        y0 = ybad.tolist().index(False) + pad[1]
        y1 = len(ybad) - ybad[::-1].tolist().index(False) - 1 - pad[1]
    else:
        bot_pads=np.repeat(np.nan,nslicer)
        top_pads=np.repeat(np.nan,nslicer)
        for i in range(nslicer):
            stripe=wl[:,i]
            stripe_clean=stripe[stripe!=0]
            if len(stripe_clean)==0:
                continue

            stripe_clean_masked,lo,hi=astropy.stats.sigma_clip(stripe_clean,sigma=nsig,return_bounds=True)
            med=np.median(stripe_clean_masked.data[~stripe_clean_masked.mask])
            thresh=((med-lo)+(hi-med))/2
            stripe_abs=np.abs(stripe-med)

            # top
            index=[]
            for j in range(1,npix,1):
                if (stripe_abs[j]>stripe_abs[j-1]) and (stripe_abs[j]>thresh):
                    index.append(j)
            if len(index)==0:
                top_pads[i]=0
            else:
                top_pads[i]=index[-1]

            # bottom
            index=[]
            for j in range(npix-2,-1,-1):
                if (stripe_abs[j]>stripe_abs[j+1]) and (stripe_abs[j]>thresh):
                    index.append(j)
            if len(index)==0:
                bot_pads[i]=0
            else:
                bot_pads[i]=index[-1]+1

        y0=np.nanmedian(bot_pads)+pad[1]
        y1=np.nanmedian(top_pads)-pad[1]

    y0=int(np.round(y0))
    y1=int(np.round(y1))
    ycrop = [y0, y1]

    if inst=='PCWI':
        x0,x1,y0,y1=y0,y1,x0,x1
        xcrop,ycrop=ycrop,xcrop

    utils.output("\tAutoCrop Parameters:\n")
    utils.output("\t\tx-crop: %02i:%02i\n" % (x0, x1))
    utils.output("\t\ty-crop: %02i:%02i\n" % (y0, y1))
    utils.output("\t\tz-crop: %i:%i (%i:%i A)\n" % (z0, z1, w0, w1))

    if plot:

        x0, x1 = xcrop
        y0, y1 = ycrop
        z0, z1 = zcrop

        xprof_clean = np.max(data[z0:z1, y0:y1, :], axis=(0, 1))
        yprof_clean = np.max(data[z0:z1, :, x0:x1], axis=(0, 2))
        zprof_clean = np.max(data[:, y0:y1, x0:x1], axis=(1, 2))

        fig, axes = plt.subplots(3, 1, figsize=(8, 8))
        xax, yax, wax = axes
        xax.step(xprof_clean, 'k-')
        lim=xax.get_ylim()
        xax.set_xlabel("X (Axis 2)", fontsize=14)
        xax.plot([x0, x0], [xprof.min(), xprof.max()], 'r-' )
        xax.plot([x1, x1], [xprof.min(), xprof.max()], 'r-' )
        xax.set_ylim(lim)

        yax.step(yprof_clean, 'k-')
        lim=yax.get_ylim()
        yax.set_xlabel("Y (Axis 1)", fontsize=14)
        yax.plot([y0, y0], [yprof.min(), yprof.max()], 'r-' )
        yax.plot([y1, y1], [yprof.min(), yprof.max()], 'r-' )
        yax.set_ylim(lim)

        wax.step(zprof_clean, 'k-')
        lim=wax.get_ylim()
        wax.plot([z0, z0], [zprof.min(), zprof.max()], 'r-' )
        wax.plot([z1, z1], [zprof.min(), zprof.max()], 'r-' )
        wax.set_xlabel("Z (Axis 0)", fontsize=14)
        wax.set_ylim(lim)
        fig.tight_layout()
        plt.show()

    return xcrop,ycrop,wcrop


def crop(fits_in, xcrop=None, ycrop=None, wcrop=None):
    """Crops an input data cube (FITS).

    Args:
        fits_in (astropy HDU / HDUList): Input HDU/HDUList with 3D data.
        xcrop (int tuple): Indices of range to crop x-axis to. Default: None.
        ycrop (int tuple): Indices of range to crop y-axis to. Default: None.
        wcrop (int tuple): Wavelength range (A) to crop cube to. Default: None.

    Returns:
        HDU / HDUList*: Trimmed FITS object with updated header.
        *Return type matches type of fits_in argument.

    Examples:

        The parameter wcrop (wavelength crop) is in Angstrom, so to crop a
        data cube to the wavelength range 4200-4400A ,the usage would be:

        >>> from astropy.io import fits
        >>> from cwitools.reduction import crop
        >>> myfits = fits.open("mydata.fits")
        >>> myfits_cropped = crop(myfits,wcrop=(4200,4400))

        Crop ranges for the x/y axes are given in image coordinates (px).
        They can be given either as straight-forward indices:

        >>> crop(myfits, xcrop=(10,60))

        Or using negative numbers to count backwards from the last index:

        >>> crop(myfits, ycrop=(10,-10))

    """

    hdu=utils.extractHDU(fits_in)
    data = fits_in.data.copy()
    header = fits_in.header.copy()

    wav_axis = coordinates.get_wav_axis(header)

    data[np.isnan(data)] = 0
    xprof = np.max(data, axis=(0, 1))
    yprof = np.max(data, axis=(0, 2))
    zprof = np.max(data, axis=(1, 2))

    if xcrop==None: xcrop=[0,-1]
    if ycrop==None: ycrop=[0,-1]
    if wcrop==None: zcrop=[0,-1]
    else:
        w0, w1 = wcrop
        if w1 == -1:
            w1 = wav_axis.max()
        zcrop = coordinates.get_indices(w0, w1,header)

    #Crop cube
    cropData = data[zcrop[0]:zcrop[1],ycrop[0]:ycrop[1],xcrop[0]:xcrop[1]]

    #Change RA/DEC/WAV reference pixels
    header["CRPIX1"] -= xcrop[0]
    header["CRPIX2"] -= ycrop[0]
    header["CRPIX3"] -= zcrop[0]

    trimmedhdu = utils.matchHDUType(fits_in, cropData, header)

    return trimmedhdu

def rotate(wcs, theta):
    """Rotate WCS coordinates to new orientation given by theta.

    Analog to ``astropy.wcs.WCS.rotateCD``, which is deprecated since
    version 1.3 (see https://github.com/astropy/astropy/issues/5175).

    Args:
        wcs (astropy.wcs.WCS): The input WCS to be rotated
        theta (float): The rotation angle, in degrees.

    Returns:
        astropy.wcs.WCS: The rotated WCS

    """
    theta = np.deg2rad(theta)
    sinq = np.sin(theta)
    cosq = np.cos(theta)
    mrot = np.array([[cosq, -sinq],
                     [sinq, cosq]])

    if wcs.wcs.has_cd():    # CD matrix
        newcd = np.dot(mrot, wcs.wcs.cd)
        wcs.wcs.cd = newcd
        wcs.wcs.set()
        return wcs
    elif wcs.wcs.has_pc():      # PC matrix + CDELT
        newpc = np.dot(mrot, wcs.wcs.get_pc())
        wcs.wcs.pc = newpc
        wcs.wcs.set()
        return wcs
    else:
        raise TypeError("Unsupported wcs type (need CD or PC matrix)")


def coadd(fitsList, pa=0, pxthresh=0.5, expthresh=0.1, verbose=False, vardata=False,
plot=False):
    """Coadd a list of fits images into a master frame.

    Args:

        fitslist (lists): List of FITS (Astropy HDUList) objects to coadd
        pxthresh (float): Minimum fractional pixel overlap.
            This is the overlap between an input pixel and a pixel in the
            output frame. If a given pixel from an input frame covers less
            than this fraction of an output pixel, its contribution will be
            rejected.
        expthresh (float): Minimum exposure time, as fraction of maximum.
            If an area in the coadd has a stacked exposure time less than
            this fraction of the maximum overlapping exposure time, it will be
            trimmed from the coadd. Default: 0.1.
        pa (float): The desired position-angle of the output data.
        verbose (bool): Show progress bars and file names.
        vardata (bool): Set to TRUE when coadding variance data.

    Returns:

        astropy.io.fits.HDUList: The stacked FITS with new header.


    Raises:

        RuntimeError: If wavelength scales of input are not equal.

    Examples:

        Basic example of coadding three cubes in your current directory:

        >>> from cwitools import reduction
        >>> myfiles = ["cube1.fits","cube2.fits","cube3.fits"]
        >>> coadded_fits = reduction.coadd(myfiles)
        >>> coadded_fits.writeto("coadd.fits")

        More advanced example, using glob to find files:

        >>> from glob import glob
        >>> from cwitools import reduction
        >>> myfiles = glob.glob("/home/user1/data/target1/*icubes.fits")
        >>> coadded_fits = reduction.coadd(myfiles)
        >>> coadded_fits.writeto("/home/user1/data/target1/coadd.fits")

    """
    #
    # STAGE 0: PREPARATION
    #

    # Extract basic header info
    hdrList    = [ f[0].header for f in fitsList ]
    wcsList    = [ WCS(h) for h in hdrList ]
    pxScales   = np.array([ proj_plane_pixel_scales(wcs) for wcs in wcsList ])

    # Get 2D headers, WCS and on-sky footprints
    h2DList    = [ coordinates.get_header2d(h) for h in hdrList]
    w2DList    = [ WCS(h) for h in h2DList ]
    footPrints = np.array([ w.calc_footprint() for w in w2DList ])

    # Exposure times
    expTimes = []
    for i,hdr in enumerate(hdrList):
        if "TELAPSE" in hdr: expTimes.append(hdr["TELAPSE"])
        else: expTimes.append(hdr["EXPTIME"])

    # Extract into useful data structures
    xScales,yScales,wScales = ( pxScales[:,i] for i in range(3) )
    pxAreas = [ (xScales[i]*yScales[i]) for i in range(len(xScales)) ]
    # Determine coadd scales
    coadd_xyScale = np.min(np.abs(pxScales[:,:2]))
    coadd_wScale  = np.min(np.abs(pxScales[:,2]))


    #
    # STAGE 1: WAVELENGTH ALIGNMENT
    #
    if verbose: utils.output("\tAligning wavelength axes...\n")
    # Check that the scale (Ang/px) of each input image is the same
    if len(set(wScales))!=1:

        raise RuntimeError("ERROR: Wavelength axes must be equal in scale for current version of code.")

    else:

        # Get common wavelength scale
        cd33 = hdrList[0]["CD3_3"]

        # Get lower and upper wavelengths for each cube
        wav0s = [ h["CRVAL3"] - (h["CRPIX3"]-1)*cd33 for h in hdrList ]
        wav1s = [ wav0s[i] + h["NAXIS3"]*cd33 for i,h in enumerate(hdrList) ]

        # Get new wavelength axis
        wNew = np.arange(min(wav0s)-cd33, max(wav1s)+cd33,cd33)

        # Adjust each cube to be on new wavelenght axis
        for i,f in enumerate(fitsList):

            # Pad the end of the cube with zeros to reach same length as wNew
            f[0].data = np.pad( f[0].data, ( (0, len(wNew)-f[0].header["NAXIS3"]), (0,0) , (0,0) ) , mode='constant' )

            # Get the wavelength offset between this cube and wNew
            dw = (wav0s[i] - wNew[0])/cd33

            # Split the wavelength difference into an integer and sub-pixel shift
            intShift = int(dw)
            spxShift = dw - intShift

            # Perform integer shift with np.roll
            f[0].data = np.roll(f[0].data,intShift,axis=0)

            # Create convolution matrix for subpixel shift (in effect; linear interpolation)
            K = np.array([ spxShift, 1-spxShift ])

            # Shift data along axis by convolving with K
            if vardata: K = K**2

            f[0].data = np.apply_along_axis(lambda m: np.convolve(m, K, mode='same'), axis=0, arr=f[0].data)

            f[0].header["NAXIS3"] = len(wNew)
            f[0].header["CRVAL3"] = wNew[0]
            f[0].header["CRPIX3"] = 1

    #
    # Stage 2 - SPATIAL ALIGNMENT
    #
    utils.output("\tMapping pixels from input-->sky-->output frames.\n")

    #Take first header as template for coadd header
    hdr0 = h2DList[0]

    #Get 2D WCS
    wcs0 = WCS(hdr0)

    #Get plate-scales
    dx0,dy0 = proj_plane_pixel_scales(wcs0)

    #Make aspect ratio in terms of plate scales 1:1
    if   dx0>dy0: wcs0.wcs.cd[:,0] /= dx0/dy0
    elif dy0>dx0: wcs0.wcs.cd[:,1] /= dy0/dx0
    else: pass

    #Set coadd canvas to desired orientation

    #Try to load orientation from header
    pa0 = None
    for rotKey in ["ROTPA","ROTPOSN"]:
        if rotKey in hdr0:
            pa0=hdr0[rotKey]
            break

    #If no value was found, set to desired PA so that no rotation takes place
    if pa0==None:
        warnings.warn("No header key for PA (ROTPA or ROTPOSN) found in first input file. Cannot guarantee output PA.")
        pa0 = pa

    #Rotate WCS to the input pa
    wcs0 = rotate(wcs0,pa0-pa)

    #Set new WCS - we will use it later to create the canvas
    wcs0.wcs.set()

    # We don't know which corner is which for an arbitrary rotation, so map each vertex to the coadd space
    x0,y0 = 0,0
    x1,y1 = 0,0
    for fp in footPrints:
        ras,decs = fp[:,0],fp[:,1]
        xs,ys = wcs0.all_world2pix(ras,decs,0)

        xMin,yMin = np.min(xs),np.min(ys)
        xMax,yMax = np.max(xs),np.max(ys)

        if xMin<x0: x0=xMin
        if yMin<y0: y0=yMin

        if xMax>x1: x1=xMax
        if yMax>y1: y1=yMax

    #These upper and lower x-y bounds to shift the canvas
    dx = int(round((x1-x0)+1))
    dy = int(round((y1-y0)+1))

    #
    ra0,dec0 = wcs0.all_pix2world(x0,y0,0)
    ra1,dec1 = wcs0.all_pix2world(x1,y1,0)

    #Set the lower corner of the WCS and create a canvas
    wcs0.wcs.crpix[0] = 1
    wcs0.wcs.crval[0] = ra0
    wcs0.wcs.crpix[1] = 1
    wcs0.wcs.crval[1] = dec0
    wcs0.wcs.set()

    hdr0 = wcs0.to_header()

    #
    # Now that WCS has been figured out - make header and regenerate WCS
    #
    coaddHdr = hdrList[0].copy()

    coaddHdr["NAXIS1"] = dx
    coaddHdr["NAXIS2"] = dy
    coaddHdr["NAXIS3"] = len(wNew)

    coaddHdr["CRPIX1"] = hdr0["CRPIX1"]
    coaddHdr["CRPIX2"] = hdr0["CRPIX2"]
    coaddHdr["CRPIX3"] = 1

    coaddHdr["CRVAL1"] = hdr0["CRVAL1"]
    coaddHdr["CRVAL2"] = hdr0["CRVAL2"]
    coaddHdr["CRVAL3"] = wNew[0]

    coaddHdr["CD1_1"]  = wcs0.wcs.cd[0,0]
    coaddHdr["CD1_2"]  = wcs0.wcs.cd[0,1]
    coaddHdr["CD2_1"]  = wcs0.wcs.cd[1,0]
    coaddHdr["CD2_2"]  = wcs0.wcs.cd[1,1]

    coaddHdr2D = coordinates.get_header2d(coaddHdr)
    coaddWCS   = WCS(coaddHdr2D)
    coaddFP = coaddWCS.calc_footprint()


    #Get scales and pixel size of new canvas
    coadd_dX,coadd_dY = proj_plane_pixel_scales(coaddWCS)
    coadd_pxArea = (coadd_dX*coadd_dY)

    # Create data structures to store coadded cube and corresponding exposure time mask
    coaddData = np.zeros((len(wNew),coaddHdr["NAXIS2"],coaddHdr["NAXIS1"]))
    coaddExp  = np.zeros_like(coaddData)

    W,Y,X = coaddData.shape

    if plot:
        fig1,ax = plt.subplots(1,1)
        for fp in footPrints:
            ax.plot( -fp[0:2,0],fp[0:2,1],'k-')
            ax.plot( -fp[1:3,0],fp[1:3,1],'k-')
            ax.plot( -fp[2:4,0],fp[2:4,1],'k-')
            ax.plot( [ -fp[3,0], -fp[0,0] ] , [ fp[3,1], fp[0,1] ],'k-')
        for fp in [coaddFP]:
            ax.plot( -fp[0:2,0],fp[0:2,1],'r-')
            ax.plot( -fp[1:3,0],fp[1:3,1],'r-')
            ax.plot( -fp[2:4,0],fp[2:4,1],'r-')
            ax.plot( [ -fp[3,0], -fp[0,0] ] , [ fp[3,1], fp[0,1] ],'r-')

        fig1.show()
        plt.waitforbuttonpress()

        plt.close()

        plt.ion()

        grid_width  = 2
        grid_height = 2
        gs = gridspec.GridSpec(grid_height,grid_width)

        fig2 = plt.figure(figsize=(12,12))
        inAx  = fig2.add_subplot(gs[ :1, : ])
        skyAx = fig2.add_subplot(gs[ 1:, :1 ])
        imgAx = fig2.add_subplot(gs[ 1:, 1: ])

    if verbose: pbar = tqdm(total=np.sum([x[0].data[0].size for x in fitsList]))

    # Run through each input frame
    for i,f in enumerate(fitsList):

        #Get shape of current cube
        w,y,x = f[0].data.shape

        # Create intermediate frame to build up coadd contributions pixel-by-pixel
        buildFrame = np.zeros_like(coaddData)

        # Fract frame stores a coverage fraction for each coadd pixel
        fractFrame = np.zeros_like(coaddData)

        # Get wavelength coverage of this FITS
        wavIndices = np.ones(len(wNew),dtype=bool)
        wavIndices[wNew < wav0s[i]] = 0
        wavIndices[wNew > wav1s[i]] = 0

        # Convert to a flux-like unit if the input data is in counts
        if "electrons" in f[0].header["BUNIT"]:

            # Scale data to be in counts per unit time
            if vardata: f[0].data /= expTimes[i]**2
            else: f[0].data /= expTimes[i]

            f[0].header["BUNIT"] = "electrons/sec"

        if plot:
            inAx.clear()
            skyAx.clear()
            imgAx.clear()
            inAx.set_title("Input Frame Coordinates")
            skyAx.set_title("Sky Coordinates")
            imgAx.set_title("Coadd Coordinates")
            imgAx.set_xlabel("X")
            imgAx.set_ylabel("Y")
            skyAx.set_xlabel("RA (hh.hh)")
            skyAx.set_ylabel("DEC (dd.dd)")
            xU,yU = x,y
            inAx.plot( [0,xU], [0,0], 'k-')
            inAx.plot( [xU,xU], [0,yU], 'k-')
            inAx.plot( [xU,0], [yU,yU], 'k-')
            inAx.plot( [0,0], [yU,0], 'k-')
            inAx.set_xlim( [-5,xU+5] )
            inAx.set_ylim( [-5,yU+5] )
            #inAx.plot(qXin,qYin,'ro')
            inAx.set_xlabel("X")
            inAx.set_ylabel("Y")
            xU,yU = X,Y
            imgAx.plot( [0,xU], [0,0], 'r-')
            imgAx.plot( [xU,xU], [0,yU], 'r-')
            imgAx.plot( [xU,0], [yU,yU], 'r-')
            imgAx.plot( [0,0], [yU,0], 'r-')
            imgAx.set_xlim( [-0.5,xU+1] )
            imgAx.set_ylim( [-0.5,yU+1] )
            for fp in footPrints[i:i+1]:
                skyAx.plot( -fp[0:2,0],fp[0:2,1],'k-')
                skyAx.plot( -fp[1:3,0],fp[1:3,1],'k-')
                skyAx.plot( -fp[2:4,0],fp[2:4,1],'k-')
                skyAx.plot( [ -fp[3,0], -fp[0,0] ] , [ fp[3,1], fp[0,1] ],'k-')
            for fp in [coaddFP]:
                skyAx.plot( -fp[0:2,0],fp[0:2,1],'r-')
                skyAx.plot( -fp[1:3,0],fp[1:3,1],'r-')
                skyAx.plot( -fp[2:4,0],fp[2:4,1],'r-')
                skyAx.plot( [ -fp[3,0], -fp[0,0] ] , [ fp[3,1], fp[0,1] ],'r-')


            #skyAx.set_xlim([ra0+0.001,ra1-0.001])
            skyAx.set_ylim([dec0-0.001,dec1+0.001])

        # Loop through spatial pixels in this input frame
        for yj in range(y):

            for xk in range(x):


                # Define BL, TL, TR, BR corners of pixel as coordinates
                inPixVertices =  np.array([ [xk-0.5,yj-0.5], [xk-0.5,yj+0.5], [xk+0.5,yj+0.5], [xk+0.5,yj-0.5] ])

                # Convert these vertices to RA/DEC positions
                inPixRADEC = w2DList[i].all_pix2world(inPixVertices,0)

                # Convert the RA/DEC vertex values into coadd frame coordinates
                inPixCoadd = coaddWCS.all_world2pix(inPixRADEC,0)

                #Create polygon object for projection of this input pixel onto coadd grid
                pixIN = Polygon( inPixCoadd )


                #Get bounding pixels on coadd grid
                xP0,yP0,xP1,yP1 = (int(x) for x in list(pixIN.bounds))


                if plot:
                    inAx.plot( inPixVertices[:,0], inPixVertices[:,1],'kx')
                    skyAx.plot(-inPixRADEC[:,0],inPixRADEC[:,1],'kx')
                    imgAx.plot(inPixCoadd[:,0],inPixCoadd[:,1],'kx')

                #Get bounds of pixel in coadd image
                xP0,yP0,xP1,yP1 = (int(round(x)) for x in list(pixIN.exterior.bounds))

                # Upper bounds need to be increased to include full pixel
                xP1+=1
                yP1+=1


                # Run through pixels on coadd grid and add input data
                for xC in range(xP0,xP1):
                    for yC in range(yP0,yP1):

                        try:
                            # Define BL, TL, TR, BR corners of pixel as coordinates
                            cPixVertices =  np.array( [ [xC-0.5,yC-0.5], [xC-0.5,yC+0.5], [xC+0.5,yC+0.5], [xC+0.5,yC-0.5] ]   )

                            # Create Polygon object and store in array
                            pixCA = box( xC-0.5, yC-0.5, xC+0.5, yC+0.5 )

                            # Calculation fractional overlap between input/coadd pixels
                            overlap = pixIN.intersection(pixCA).area/pixIN.area

                            # Add fraction to fraction frame
                            fractFrame[wavIndices, yC, xC] += overlap

                            if vardata: overlap=overlap**2

                            # Add data to build frame
                            # Wavelength axis has been padded with zeros already
                            buildFrame[wavIndices, yC, xC] += overlap*f[0].data[wavIndices, yj, xk]

                        except: continue


                if verbose: pbar.update(1)
        if plot:
            fig2.canvas.draw()
            plt.waitforbuttonpress()

        #Calculate ratio of coadd pixel area to input pixel area
        pxAreaRatio = coadd_pxArea/pxAreas[i]

        # Max value in fractFrame should be pxAreaRatio - it's the biggest fraction of an input pixel that can add to one coadd pixel
        # We want to use this map now to create a flatFrame - where the values represent a covering fraction for each pixel
        flatFrame = fractFrame/pxAreaRatio

        #Replace zero-values with inf values to avoid division by zero when flat correcting
        flatFrame[flatFrame==0] = np.inf

        #Perform flat field correction for pixels that are not fully covered
        buildFrame /= flatFrame

        #Zero any pixels below user-set pixel threshold, and set flat value to inf
        buildFrame[flatFrame<pxthresh] = 0
        flatFrame[flatFrame<pxthresh] = np.inf

        # Create 3D mask of non-zero voxels from this frame
        M = flatFrame<np.inf

        # Add weight*data to coadd (numerator of weighted mean with exptime as weight)
        if vardata: coaddData += (expTimes[i]**2)*buildFrame
        else: coaddData += expTimes[i]*buildFrame

        #Add to exposure mask
        coaddExp += expTimes[i]*M
        coaddExp2D = np.sum(coaddExp,axis=0)

    if verbose:
        pbar.close()

    utils.output("\tTrimming coadded canvas.\n")

    if plot: plt.close()

    # Create 1D exposure time profiles
    expSpec = np.mean(coaddExp,axis=(1,2))
    expXMap = np.mean(coaddExp,axis=(0,1))
    expYMap = np.mean(coaddExp,axis=(0,2))

    # Normalize the profiles
    expSpec/=np.max(expSpec)
    expXMap/=np.max(expXMap)
    expYMap/=np.max(expYMap)

    # Convert 0s to 1s in exposure time cube
    ee = coaddExp.flatten()
    ee[ee==0] = 1
    coaddExp = np.reshape( ee, coaddData.shape )

    # Divide by sum of weights (or square of sum)

    if vardata: coaddData /= coaddExp**2
    else: coaddData /= coaddExp

    # Create FITS object
    coaddHDU = fits.PrimaryHDU(coaddData)
    coaddFITS = fits.HDUList([coaddHDU])
    coaddFITS[0].header = coaddHdr

    #Exposure time threshold, relative to maximum exposure time, below which to crop.
    useW = expSpec>expthresh
    useX = expXMap>expthresh
    useY = expYMap>expthresh

    #Trim the data
    coaddFITS[0].data = coaddFITS[0].data[useW]
    coaddFITS[0].data = coaddFITS[0].data[:,useY]
    coaddFITS[0].data = coaddFITS[0].data[:,:,useX]

    #Get 'bottom/left/blue corner of cropped data
    W0 = np.argmax(useW)
    X0 = np.argmax(useX)
    Y0 = np.argmax(useY)

    #Update the WCS to account for trimmed pixels
    coaddFITS[0].header["CRPIX3"] -= W0
    coaddFITS[0].header["CRPIX2"] -= Y0
    coaddFITS[0].header["CRPIX1"] -= X0

    #Create FITS for variance data if we are propagating that
    return coaddFITS




def air2vac(fits_in,mask=False):
    """Covert wavelengths in a cube from standard air to vacuum.

    Args:
        fits_in (astropy HDU / HDUList): Input HDU/HDUList with 3D data.
        mask (bool): Set if the cube is a mask cube.

    Returns:
        HDU / HDUList*: Trimmed FITS object with updated header.
        *Return type matches type of fits_in argument.

    """

    hdu=utils.extractHDU(fits_in)
    hdu=hdu.copy()
    cube=np.nan_to_num(hdu.data,nan=0,posinf=0,neginf=0)
    hdr=hdu.header

    if hdr['CTYPE3']=='WAVE':
        utils.output("\tFITS already in vacuum wavelength.\n")
        return fits_in

    wave_air=coordinates.get_wav_axis(hdr)
    wave_vac=pyasl.airtovac2(wave_air)

    # resample to uniform grid
    cube_new=np.zeros_like(cube)
    for i in range(cube.shape[2]):
        for j in range(cube.shape[1]):
            spec0=cube[:,j,i]
            if mask==False:
                f_cubic=interp1d(wave_vac,spec0,kind='cubic',fill_value='extrapolate')
                spec_new=f_cubic(wave_air)
            else:
                f_pre=interp1d(wave_vac,spec0,kind='previous',bounds_error=False,fill_value=128)
                spec_pre=f_pre(wave_air)
                f_nex=interp1d(wave_vac,spec0,kind='next',bounds_error=False,fill_value=128)
                spec_nex=f_nex(wave_air)

                spec_new=np.zeros_like(spec0)
                for k in range(spec0.shape[0]):
                    spec_new[k]=max(spec_pre[k],spec_nex[k])
            cube_new[:,j,i]=spec_new

    hdr['CTYPE3']='WAVE'

    hdu_new=utils.matchHDUType(fits_in, cube_new, hdr)

    return hdu_new



def heliocentric(fits_in, mask=False, return_vcorr=False, resample=True, vcorr=None, barycentric=False):
    """Apply heliocentric correction to the cubes.

    Args:
        fits_in (astropy HDU / HDUList): Input HDU/HDUList with
            3D data.
        mask (bool): Set if the cube is a mask cube. This only
            works for resampled cubes.
        return_vcorr (bool): If set, return the correction velocity
            (in km/s) as well.
        resample (bool): Resample the cube to the original wavelength
            grid?
        vcorr (float): Use a different correction velocity.
        barycentric (bool): Use barycentric correction instead of helocentric.


    Returns:
        HDU / HDUList*: Trimmed FITS object with updated header.
        vcorr (float): Correction velocity in km/s. Only returns if vcorr
            is set to True.
        *Return type matches type of fits_in argument.

    """

    hdu=utils.extractHDU(fits_in)
    hdu=hdu.copy()
    cube=np.nan_to_num(hdu.data,nan=0,posinf=0,neginf=0)
    hdr=hdu.header

    v_old=0.
    if 'VCORR' in hdr:
        v_old=hdr['VCORR']
        utils.output("\tRolling back the existing correction with:\n")
        utils.output("\t\tVcorr = %.2f km/s.\n" % (v_old))

    if vcorr is None:
        targ=astropy.coordinates.SkyCoord(hdr['TARGRA'],hdr['TARGDEC'],unit='deg',obstime=hdr['DATE-BEG'])
        keck=astropy.coordinates.EarthLocation.of_site('Keck Observatory')
        if barycentric:
            vcorr=targ.radial_velocity_correction(kind='barycentric',location=keck)
        else:
            vcorr=targ.radial_velocity_correction(kind='heliocentric',location=keck)
        vcorr=vcorr.to('km/s').value

    utils.output("\tHelio/Barycentric correction:\n")
    utils.output("\t\tVcorr = %.2f km/s.\n" % (vcorr))

    v_tot=vcorr-v_old

    if resample==False:
        hdr['CRVAL3']=hdr['CRVAL3']*(1+v_tot/2.99792458e5)
        hdr['CD3_3']=hdr['CD3_3']*(1+v_tot/2.99792458e5)
        hdr['VCORR']=vcorr
        hdu_new=utils.matchHDUType(fits_in, cube, hdr)
        if not return_vcorr:
            return hdu_new
        else:
            return hdu_new,vcorr

    else:

        wave_old=coordinates.get_wav_axis(hdr)
        wave_hel=wave_old*(1+v_tot/2.99792458e8)

        # resample to uniform grid
        cube_new=np.zeros_like(cube)
        for i in range(cube.shape[2]):
            for j in range(cube.shape[1]):
                spec0=cube[:,j,i]
                if mask==False:
                    f_cubic=interp1d(wave_hel,spec0,kind='cubic',fill_value='extrapolate')
                    spec_new=f_cubic(wave_old)
                else:
                    f_pre=interp1d(wave_hel,spec0,kind='previous',bounds_error=False,fill_value=128)
                    spec_pre=f_pre(wave_old)
                    f_nex=interp1d(wave_hel,spec0,kind='next',bounds_error=False,fill_value=128)
                    spec_nex=f_nex(wave_old)

                    spec_new=np.zeros_like(spec0)
                    for k in range(spec0.shape[0]):
                        spec_new[k]=max(spec_pre[k],spec_nex[k])
                cube_new[:,j,i]=spec_new

        hdr['VCORR']=vcorr
        hdu_new=utils.matchHDUType(fits_in, cube_new, hdr)

        if not return_vcorr:
            return hdu_new
        else:
            return hdu_new,vcorr
