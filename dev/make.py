
from astropy import units as u
from astropy.cosmology import WMAP9 as cosmo
from astropy.io import fits
from astropy.modeling import models,fitting
from astropy.nddata import Cutout2D
from astropy.stats import SigmaClip
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_scales
from astropy.visualization.mpl_normalize import ImageNormalize
from photutils import CircularAperture
from photutils import DAOStarFinder
from scipy.stats import sigmaclip
from scipy.ndimage.measurements import center_of_mass as CoM
from scipy.optimize import differential_evolution
from scipy.signal import medfilt

import astropy.convolution as astConvolve
import matplotlib.pyplot as plt
import argparse
import numpy as np
import pyregion
import sys
import time

import libs



#Timer start
tStart = time.time()

# Use python's argparse to handle command-line input
parser = argparse.ArgumentParser(description='Make products from data cubes and object masks.')
mainGroup = parser.add_argument_group(title="Main",description="Basic input")
mainGroup.add_argument('cube',
                    type=str,
                    metavar='cube',
                    help='The input data cube.'
)

objGroup = parser.add_argument_group(title="Objects")
objGroup.add_argument('-obj',
                    type=str,
                    metavar='path',
                    help='Object Mask cube.',
)
objGroup.add_argument('-var',
                    type=str,
                    metavar='path',
                    help='Variance cube (to calculate error maps).',
)
objGroup.add_argument('-objID',
                    type=str,
                    metavar='str',
                    help='The ID of the object to use. Use -1 for all objects. Can also provide multiple as comma-separated list.',
                    default='-1'
)
objGroup.add_argument('-zSNR',
                    type=float,
                    metavar='float',
                    help='Minimum integrated SNR of a spaxel spectrum before using to calculate velocity (Default 3)',
                    default=3
)
objGroup.add_argument('-nl',
                    type=int,
                    metavar='int',
                    help='The wavelength layer to sample noise from for pseudoNB.',
                    default=None
)
imgGroup = parser.add_argument_group(title="Image Settings")
imgGroup.add_argument('-type',
                    type=str,
                    metavar='str',
                    help='Type of image to be made: wl=white light, nb=pseudo-NB, vel=velocity (0th and 1st moment, tri=nb+vel+spc). Default is white-light image.',
                    default='wl',
                    choices=['wl','nb','vel','spc','tri']
)
imgGroup.add_argument('-wav0',
                    type=float,
                    metavar='float',
                    help='If making velocity map - you can set the central wavelength with this.'
)
imgGroup.add_argument('-par',
                    type=str,
                    metavar='path',
                    help='Center the image on a target using CWITools parameter file.',
                    default=None
)
imgGroup.add_argument('-boxSize',
                    type=float,
                    metavar='float',
                    help='If using -par, this determines the box size around the target in pkpc or pixels. Set unit with -boxUnit.'
)
imgGroup.add_argument('-boxUnit',
                    type=str,
                    metavar='str',
                    help='Unit for -boxSize setting [px/arcsec/pkpc]. Defaults to px.',
                    choices=['px','pkpc','arcsec'],
                    default='px'
)
imgGroup.add_argument('-zmask',
                    type=str,
                    metavar='int tuple',
                    help='Z-indices to mask when making WL image (e.g. \'21,32\')',
                    default='0,0'
)
imgGroup.add_argument('-zunit',
                    type=str,
                    metavar='str',
                    help='Unit of input for zmask. Can be Angstrom (A) or Pixels (px) (Default: A)',
                    default='A',
                    choices=['A','px']
)
imgGroup.add_argument('-wSmooth',
                    type=float,
                    metavar='str',
                    help='Wavelength smoothing kernel radius (pixels). Default: None.',
)
imgGroup.add_argument('-wkernel',
                    type=str,
                    metavar='str',
                    help='Type of kernel to use for wavelength smoothing',
                    default='box',
                    choices=['box','gaussian']
)
imgGroup.add_argument('-rSmooth',
                    type=float,
                    metavar='str',
                    help='Wavelength smoothing kernel radius (pixels). Default: None'
)
imgGroup.add_argument('-rkernel',
                    type=str,
                    metavar='str',
                    help='Type of kernel to use for wavelength smoothing',
                    default='box',
                    choices=['box','gaussian']
)
args = parser.parse_args()

## PARSE PARAMETERS AND LOAD DATA

#Try to load the fits file
try:
    F = fits.open(args.cube)
    print("Loaded intensity cube: %s"%args.cube)
except: print("Error: could not open '%s'\nExiting."%args.cube);sys.exit()

#Try to load the fits file
usevar=False
try:
    VF = fits.open(args.var)
    print("Loaded variance cube: %s"%args.var)
    usevar=True
except: pass

#Try to parse the wavelength mask tuple
try: z0,z1 = tuple(int(x) for x in args.zmask.split(','))
except: print("Could not parse zmask argument. Should be two comma-separated integers (e.g. 21,32)");sys.exit()

#Extract useful stuff and create useful data structures
cube  = F[0].data
if usevar: var = VF[0].data
w,y,x = cube.shape
h3D   = F[0].header
h2D   = libs.cubes.get2DHeader(h3D)
wcs   = WCS(h2D)
wav   = libs.cubes.getWavAxis(h3D)

#Apply spatial smoothing to cube if option is set
if args.rSmooth!=None:
    cube = libs.science.smooth3D(cube,args.rSmooth,ktype=args.rkernel,axes=(1,2))
    if usevar: var = libs.science.smooth3D(var,args.rSmooth,ktype=args.rkernel,axes=(1,2),var=True)

#Apply wavelength smoothing if option is set
if args.wSmooth!=None:
    cube = libs.science.smooth3D(cube,args.wSmooth,ktype=args.wkernel,axes=[0])
    if usevar: var = libs.science.smooth3D(var,args.wSmooth,ktype=args.wkernel,axes=[0],var=True)

pxScales = proj_plane_pixel_scales(wcs)
xScale,yScale = (pxScales[0]*u.deg).to(u.arcsec), (pxScales[1]*u.degree).to(u.arcsec)
pxArea   = ( xScale*yScale ).value

#If -par flag is given - make a new cube/header with the given size/center
if args.par!=None:
    try: params = libs.params.loadparams(args.par)
    except: print("Could not open parameter file (-par flag). Please check path and try again.");sys.exit()

    xC,yC = wcs.all_world2pix(params["RA"],params["DEC"],0)

    #If user did not give boxSize, take largest spatial axis
    if args.boxSize==None:
        print("No -boxSize given with -par flag. Using maximum dimension.")
        args.boxSize = max(y,x)

    #Otherwise...
    else:

        #If unit for boxsize is arcseconds - convert to pixels
        if args.boxUnit=='arcsec': args.boxSize /= xScale.value

        #If unit for boxsize is proper kpc - convert to pixels
        elif args.boxUnit=='pkpc':
            pkpc_per_arcsec = cosmo.kpc_proper_per_arcmin(params["Z"])/60.0
            pkpc_per_pixel  = xScale*pkpc_per_arcsec
            args.boxSize /= pkpc_per_pixel.value

        #If the unit is already in pixels - do nothing ( code is just for structural clarity)
        else: pass

    #Crop cube to this 2D region
    bSize = int(round(args.boxSize))
    cubeC = np.zeros((w,bSize,bSize))
    for wi in range(w):
        cutout = Cutout2D(cube[wi],(xC,yC),bSize,wcs,mode='partial',fill_value=0)
        cubeC[wi] = cutout.data
        if wi==0: hdrNew = cutout.wcs.to_header()

    #Replace cube and update wcs/dim values
    cube  = cubeC
    w,y,x = cubeC.shape
    wcs   = cutout.wcs

    #Update 3D header with new WCS values
    for i in [1,2]:
        h3D["CRVAL%i"%i] = hdrNew["CRVAL%i"%i]
        h3D["CRPIX%i"%i] = hdrNew["CRPIX%i"%i]

    #Update 2D header
    h2D = libs.cubes.get2DHeader(h3D)

#General Prep
def fwhm2sig(fwhm): return fwhm/(2*np.sqrt(2*np.log(2)))
def gaussian(x,pars): return pars[0]*np.exp( -((x-pars[1])**2)/(2*pars[2]**2))
def sumofsquares(params,x,y): return np.sum( (y - gaussian(x,params))**2 )

#Convert cube to units of surface brightness (per arcsec2)
cube /= pxArea

#Convert cube to units of integrated flux (e.g. F_lambda*delta_lambda)
cube *= h3D["CD3_3"]

if usevar:
     var /= pxArea**2
     cube *= h3D["CD3_3"]**2

#Now to make the product
if args.type=='wl':

    #Convert zmask to pixels if given in angstrom
    if args.zunit=='A': z0,z1 = libs.cubes.getband(z0,z1,h3D)

    #Mask cube
    cube[z0:z1] = 0

    #Sum along wavelength axis
    img = np.sum(cube,axis=0)

    useX = np.sum(img,axis=0)!=0
    useY = np.sum(img,axis=1)!=0

    for yi in range(y):
        if useY[yi]:
            med = np.median(sigmaclip(img[yi,useX],high=2.5)[0])
            if np.isnan(med): continue
            img[yi,useX] -= med

    for xi in range(x):
        if useX[xi]:
            med = np.median(sigmaclip(img[useY,xi],high=2.5)[0])
            if np.isnan(med): continue
            img[useY,xi] -= med

    #Adjust values take into account in
    F[0].data = img
    F[0].header = libs.cubes.get2DHeader(h3D)
    F.writeto(args.cube.replace('.fits','.WL.fits'),overwrite=True)
    print("Saved %s"%args.cube.replace('.fits','.WL.fits'))

#If user wants to make pseudo NB image or velocity map
elif args.type in ['nb','vel','spc','tri']:

    #Load object info
    if args.obj==None: print("Must provide object mask (-obj) if you want to make a pseudo-NB or velocity map of an object.");sys.exit()
    try: O = fits.open(args.obj)

    except: print("Error opening object mask: %s"%args.obj);sys.exit()
    try: objIDs = list( int(x) for x in args.objID.split(',') )
    except: print("Could not parse -objID list. Should be int or comma-separated list of ints.");sys.exit()


    #If object info is loaded - now turn object mask into binary mask using objIDs
    idCube = O[0].data
    if objIDs==[-1]: idCube[idCube>0] = 1
    elif objIDs==[-2]: idCube[idCube>0] = 0
    else:
        for obj_id in objIDs:
            idCube[idCube==obj_id] = -99
        idCube[idCube>0] = 0
        idCube[idCube==-99] = 1

    #Crop idCube if cropping was performed on input cube
    if args.par!=None:
        idCubeC = np.zeros((w,bSize,bSize))
        for wi in range(w):
            cutout = Cutout2D(idCube[wi],(xC,yC),bSize,wcs,mode='partial',fill_value=0)
            idCubeC[wi] = cutout.data
        idCube = idCubeC

    #Create copy of input cube with non-object voxels set to zero
    objCube = cube.copy()
    objCube[idCube==0] = 0

    #Get 2D mask of useable spaxels
    msk2D = np.max(idCube,axis=0).astype(bool)

    objNB = np.sum(objCube,axis=0)

    #Get 1D wavelength mask
    msk1D = np.max(idCube,axis=(1,2))
    comZ  = CoM(msk1D)[0]
    try: comZ = int(round(comZ))
    except: comZ = w/2

    #Now use binary mask to generate the requested data product
    if args.type=='nb' or args.type=='tri':

        #Set noiselayer (-nl) if not set
        if args.nl==-1: args.nl=comZ

        stddev = np.std(cube[args.nl])

        if args.nl!=None: objNB[msk2D==0] = ( cube[args.nl][msk2D==0] )

        nbFITS = fits.HDUList([fits.PrimaryHDU(objNB)])
        nbFITS[0].header = h2D
        nbFITS.writeto(args.cube.replace('.fits','.NB.fits'),overwrite=True)
        print("Saved %s"%args.cube.replace('.fits','.NB.fits'))

        nbFITS = fits.HDUList([fits.PrimaryHDU(msk2D)])
        nbFITS[0].header = h2D
        nbFITS.writeto(args.cube.replace('.fits','.M2D.fits'),overwrite=True)
        print("Saved %s"%args.cube.replace('.fits','.M2D.fits'))

    if args.type=='vel' or args.type=='tri':

        var[var<0]=np.inf
        xcube = cube/var
        xvar  = 1/var

        #Create canvas for both first and zeroth (f,z) moments
        m0map = np.zeros_like(msk2D,dtype=float)
        m1map = np.zeros_like(m0map)

        if usevar: m0ErrMap = np.zeros_like(m0map)

        #Only make real velmaps if there is an object given
        if np.count_nonzero(idCube)>0:

            #Get spaxels that are in 2D mask
            useY,useX = np.where(msk2D==1)

            #Run through each
            for j in range(len(useX)):

                #Get x,y position
                y,x = useY[j],useX[j]

                #Get spectrum
                spec = cube[:,y,x]

                #Extract object wav-mask and crop wav/spec to that range
                mskZ = idCube[:,y,x].astype(bool)

                wav_j  = wav[mskZ]
                spec_j = spec[mskZ]

                #Calculate intensity-weighted mean
                wavMean_num = np.sum( spec_j*wav_j )
                wavMean_den = np.sum( spec_j )
                wavMean = wavMean_num/wavMean_den

                #Calculate intensity weighted dispersion
                wavVar_num = np.sum( spec_j*np.power(wav_j-wavMean,2) )
                wavVar_den = np.sum( spec_j )
                wavVar = wavVar_num/wavVar_den
                wavDisp = np.sqrt(wavVar)

                if ~np.isnan(wavMean):

                    m0map[y,x] = wavMean
                    m1map[y,x] = wavDisp

                    if usevar:
                        var_j = var[mskZ,y,x]
                        m0Err_num = wavMean_den*np.sum(spec_j*var_j) - wavMean_num*np.sum(var_j)
                        m0Err_den = np.power(wavMean_den,2)
                        m0Err = m0Err_num/m0Err_den
                        m0ErrMap[y,x] = np.sqrt(m0Err)
                else:
                    msk2D[y,x] = 0

            m0ref  = np.average(m0map[msk2D],weights=objNB[msk2D])
            #print m0ref
            #m0map[msk2D] -= m0ref
            #m0map[msk2D] *= (3e5/m0ref)
            #m1map[msk2D] *= (3e5/m0ref)

            m0map[~msk2D] = -5000
            m1map[~msk2D] = -5000

        #If no objects in input
        else:

            m0map -= 5000
            m1map -= 5000


        #Save FITS images
        m0FITS = fits.HDUList([fits.PrimaryHDU(m0map)])
        m0FITS[0].header = h2D
        m0FITS[0].header["BUNIT"] = "km/s"
        m0FITS[0].header["M0REF"] = m0ref
        m0FITS.writeto(args.cube.replace('.fits','.V0.fits'),overwrite=True)
        print("Saved %s"%args.cube.replace('.fits','.V0.fits'))

        if usevar:
            m0FITS = fits.HDUList([fits.PrimaryHDU(m0ErrMap)])
            m0FITS[0].header = h2D
            m0FITS[0].header["BUNIT"] = "km/s"
            m0FITS.writeto(args.cube.replace('.fits','.V0Err.fits'),overwrite=True)
            print("Saved %s"%args.cube.replace('.fits','.V0Err.fits'))


        m1FITS = fits.HDUList([fits.PrimaryHDU(m1map)])
        m1FITS[0].header = h2D
        m1FITS[0].header["BUNIT"] = "km/s"
        m1FITS.writeto(args.cube.replace('.fits','.V1.fits'),overwrite=True)
        print("Saved %s"%args.cube.replace('.fits','.V1.fits'))

    if args.type=='spc' or args.type=='tri':

        #Revert cube to flux/px
        cube *= pxArea

        #Revert to units of 'per Angstrom'
        cube /= h3D["CD3_3"]

        #Create cube where non-object spaxels are zeroed out
        cubeT = cube.T.copy()
        cubeT[msk2D.T==0] = 0

        #Get object spectrum summed over 2D mask
        objSpc = np.sum(cubeT,axis=(0,1))

        #Get upper and lower bounds of 3D mask projected to 1D for this spectrum
        if np.sum(msk1D)>0:
            wavMsk = wav[msk1D>0]
            mskW0,mskW1 = wavMsk[0],wavMsk[-1]
        else:
            mskW0,mskW1 = 0,0

        #Save to FITS and write 1D header
        spcFITS = fits.HDUList([fits.PrimaryHDU(objSpc)])
        spcFITS[0].header = libs.cubes.get1DHeader(h3D)
        spcFITS[0].header["MSKW0"] = mskW0
        spcFITS[0].header["MSKW1"] = mskW1
        spcFITS.writeto(args.cube.replace('.fits','.SPC.fits'),overwrite=True)
        print("Saved %s"%args.cube.replace('.fits','.SPC.fits'))
