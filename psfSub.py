from astropy import units as u
from astropy.io import fits
from astropy.modeling import models,fitting
from astropy.nddata import Cutout2D
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_scales
from astropy.stats import sigma_clip

import argparse
import numpy as np
import pyregion
import sys
import time

import libs

#Timer start
tStart = time.time()

# Use python's argparse to handle command-line input
parser = argparse.ArgumentParser(description='Perform PSF subtraction on a data cube.')
mainGroup = parser.add_argument_group(title="Main",description="Basic input")
mainGroup.add_argument('cube', 
                    type=str, 
                    metavar='cube',             
                    help='The cube to be PSF subtracted.'
)
srcGroup = parser.add_mutually_exclusive_group(required=True)
srcGroup.add_argument('-reg',
                    type=str,
                    metavar='Region File',
                    help='Region file of sources to subtract.',
                    default=None
)
srcGroup.add_argument('-pos',
                    type=str,
                    metavar='Position',
                    help='Position of source (x,y) to subtract.',
                    default=None
)
methodGroup = parser.add_argument_group(title="Method",description="Parameters related to PSF subtraction methods.")
methodGroup.add_argument('-rmin',
                    type=float,  
                    metavar='Fit Radius',  
                    help='Radius (arcsec) used to FIT the PSF model (default 1)',
                    default=1
)
methodGroup.add_argument('-rmax',
                    type=float,  
                    metavar='Sub Radius',  
                    help='Radius (arcsec) of subtraction area (default 3).',
                    default=1
)
methodGroup.add_argument('-window',
                    type=int,  
                    metavar='PSF Window',  
                    help='Window (angstrom) used to create WL image of PSF (default 150).',
                    default=150
)
methodGroup.add_argument('-dw',
                    type=int,  
                    metavar='Local PSF Window',  
                    help='Use this many extra layers around each wavelength layer to construct local PSF for fitting (default 0 - i.e. only fit to current layer)',
                    default=0
)
methodGroup.add_argument('-zmask',
                    type=str,
                    metavar='Wav Mask',
                    help='Z-indices to mask when fitting or median filtering (e.g. \'21,32\')',
                    default='0,0'
)
methodGroup.add_argument('-recenter',
                    type=bool,
                    metavar='Recenter',
                    help='Auto-recenter the input positions using PSF centroid',
                    default=True
)
fileIOGroup = parser.add_argument_group(title="File I/O",description="File input/output options.")
fileIOGroup.add_argument('-var', 
                    type=str, 
                    metavar='varCube',             
                    help='The variance cube associated with input cube - used to propagate error.',
                    default=None
)
fileIOGroup.add_argument('-ext',
                    type=str,
                    metavar='File Extension',
                    help='Extension to append to subtracted cube (.ps.fits)',
                    default='.ps.fits'
)
fileIOGroup.add_argument('-savePSF',
                    type=bool,
                    metavar='Save PSFCube',
                    help='Set to True to output PSF Cube)',
                    default=False
)
fileIOGroup.add_argument('-extPSF',
                    type=bool,
                    metavar='PSF Extension',
                    help='Extension to append to PSF cube (.ps.PSF.fits)',
                    default='.ps.PSF.fits'
)
fileIOGroup.add_argument('-saveMask',
                    type=bool,
                    metavar='Save PSFCube',
                    help='Set to True to output 2D Source Mask',
                    default=True
)
fileIOGroup.add_argument('-extMask',
                    type=str,
                    metavar='Mask Extension',
                    help='Extension to append to mask file (.ps.MASK.fits)',
                    default='.ps.MASK.fits'
)
args = parser.parse_args()

#Try to load the fits file
try: F = fits.open(args.cube)
except: print("Error: could not open '%s'\nExiting."%args.cube);sys.exit()

#Try to parse the wavelength mask tuple
try: z0,z1 = tuple(int(x) for x in args.zmask.split(','))
except: print("Could not parse zmask argument. Should be two comma-separated integers (e.g. 21,32)");sys.exit()
            
#Try loading variance cube
propVar=False
if args.var!=None:
    try: vFits = fits.open(args.var)
    except: print("Error opening varcube ('%s')" % settings["var"]); sys.exit()       
    V = vFits[0].data
    propVar=True
            
#Open fits image and extract info
hdr  = F[0].header
wcs = WCS(hdr)
pxScales = proj_plane_pixel_scales(wcs)
in_cube = F[0].data.copy()
wl_cube = in_cube.copy()
wl_cube[z0:z1] = 0

#Get sources from region file or position input
sources = []
if args.pos==None:
    try: regFile = pyregion.open(args.reg)
    except: print("Error opening region file! Double-check path and try again.");sys.exit()
    for src in regFile:
        ra,dec,pa = src.coord_list
        xP,yP,wP = wcs.all_world2pix(ra,dec,hdr["CRVAL3"],0)
        sources.append((xP,yP))    
elif args.reg==None:
    try: pos = tuple(float(x) for x in args.zmask.split(','))
    except: print("Could not parse position argument. Should be two comma-separated floats (e.g. 45.2,33.6)");sys.exit() 
    sources = [ pos ]
    
#Create cube for psfModel
model = np.zeros_like(in_cube)
w,y,x = in_cube.shape
W,Y,X = np.arange(w),np.arange(y),np.arange(x)
mask  = np.zeros((y,x))

#Convert plate scale to arcseconds
xScale,yScale = (pxScales[:2]*u.deg).to(u.arcsecond)
zScale = (pxScales[2]*u.meter).to(u.angstrom)

#Convert fitting & subtracting radii to pixel values
rMin_px = args.rmin/xScale.value
rMax_px = args.rmax/xScale.value
delZ_px = int(round(0.5*args.window/zScale.value))

#Get fitter for PSF fit
fitter = fitting.LevMarLSQFitter()

#Create main WL image for PSF re-centering
wlImg   = np.sum(wl_cube,axis=0)
wlImg  /= np.max(wlImg)
boxSize = 3*int(round(rMax_px))
yy,xx   = np.mgrid[:boxSize, :boxSize]

#Get default PSF model for re-centering
psfModel = models.Gaussian2D(amplitude=1,x_mean=boxSize/2,y_mean=boxSize/2)

#Get fitter for PSF re-centering
fitter   = fitting.LevMarLSQFitter()

import matplotlib.pyplot as plt
#Run through sources
for (xP,yP) in sources:
    
    #Get meshgrid of distance from P
    YY,XX = np.meshgrid(X-xP,Y-yP)
    RR    = np.sqrt(XX**2 + YY**2)

    if np.min(RR)>rMin_px: continue
    else:
    
        #Get cut-out around source
        psfBox = Cutout2D(wlImg,(xP,yP),(boxSize,boxSize),mode='partial',fill_value=-99).data
        
        #Get useable spaxels
        fitXY = np.array( psfBox!=-99, dtype=int)

        #Run fit
        psfFit = fitter(psfModel,yy,xx,psfBox,weights=fitXY)

        #Get sigma/fwhm
        xfwhm,yfwhm = 2.355*psfFit.x_stddev.value, 2.355*psfFit.y_stddev.value
        
        #We take larger of the two for our purposes
        fwhm = max(xfwhm,yfwhm)
        
        #Get peak SNR of this 2D PSF
        snr = np.max(psfFit(yy,xx))/np.std(sigma_clip(psfBox))
      
        #Only continue with well-fit, high-snr sources
        if snr>5 and fitter.fit_info['nfev']<100 and fwhm<10/xScale.value: 

            #Update position with fitted center
            #Note - X and Y are reversed here in the convention that cube shape is W,Y,X
            yP, xP = psfFit.x_mean.value+yP-boxSize/2, psfFit.y_mean.value+xP-boxSize/2
            
            #Update meshgrid of distance from P
            YY,XX = np.meshgrid(X-xP,Y-yP)
            RR    = np.sqrt(XX**2 + YY**2)
    
            #Get half-width-half-max
            hwhm = fwhm/2.0
            
            #Add source to mask
            mask[RR<hwhm] = 1

            #Get boolean masks for
            fitPx = RR<=rMin_px
            subPx = RR<=rMax_px

            #Run through wavelength layers
            for wi in range(w):
                
                #Get this wavelenght layer and subtract any median residual
                wl1,wl2 = max(0,wi-args.dw), min(w,wi+args.dw)+1
                layer = np.mean(in_cube[wl1:wl2],axis=0) 
                layer-= np.median(layer)

                #Get upper and lower-bounds for creating WL image
                a = max(0,wi-delZ_px)
                b = min(w,a+delZ_px)

                #Create PSF image
                psfImg = np.mean(wl_cube[a:b],axis=0)
                med    = np.median(psfImg)
                psfImg -= med

                #Extract portion of image used for fitting scaling factor
                psfImgFit = psfImg[fitPx]
                layerFit  = layer[fitPx]

                #Create initial guess using Astropy Scale 
                scaleGuess = models.Scale(factor=np.max(layerFit)/np.max(psfImgFit))

                #Fit
                scaleFit = fitter(scaleGuess,psfImgFit,layerFit)
                                   
                #Extract fit value    
                A = scaleFit.factor.value
                
                #Subtract fit from data 
                F[0].data[wi][subPx] -= A*psfImg[subPx]

                #Add to PSF model
                model[wi][subPx] += A*psfImg[subPx]
                
                #Propagate error if requested
                if propVar: V[wi][subPx] += (A**2)*psfImg[subPx]/w
                
    
outFileName = args.cube.replace('.fits',args.ext)
F.writeto(outFileName,overwrite=True)
print("Saved {0}".format(outFileName))

if args.savePSF:
    psfOut  = args.cube.replace('.fits',args.extPSF)
    psfFits = fits.HDUList([fits.PrimaryHDU(model)])
    psfFits[0].header = hdr
    psfFits.writeto(psfOut,overwrite=True)
    print("Saved {0}.".format(psfOut))

if args.saveMask:
    mskOut  = args.cube.replace('.fits',args.extMask)
    psfMask = fits.HDUList([fits.PrimaryHDU(mask)])
    psfMask[0].header = libs.cubes.get2DHeader(hdr)
    psfMask.writeto(mskOut,overwrite=True)
    print("Saved {0}.".format(mskOut))
    
if propVar:
    varOut = outFilename.replace('.fits','.var.fits')
    vFits[0].data = V
    vFits.writeto(varOut,overwrite=True)
    print("Saved {0}.".format(varOut))
    
#Timer end
tFinish = time.time()
print("Elapsed time: %.2f seconds" % (tFinish-tStart))        
