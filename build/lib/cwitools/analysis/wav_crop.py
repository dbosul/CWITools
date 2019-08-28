from .. imports libs

from astropy.io import fits as fits

import argparse
import numpy as np
import sys


def run(cubePath,wavPair,fileExt=".wCrop.fits"):
    """Crop a data cube to a given wavelength range.

    Args:
        cubePath (str): Cube to be cropped.
        wavPair (float tuple): Wavelength range to crop to, in Angstrom.
        fileExt (str): File extension for output cube (Def: .wCrop.fits)
        
    """
    #Try to load the fits file
    try: F = fits.open(cubePath)
    except: print("Error: could not open '%s'\nExiting."%cubePath);sys.exit()

    #Try to parse wavelength tuple
    try: w0,w1 = (float(x) for x in wavPair.split(','))
    except:
        print("Could not parse wavelengths from input. Please check syntax (should be comma-separated tuple of floats representing upper/lower bound in wavelength for cropped cube.")
        sys.exit();

    #Get indices of upper and lower bound
    a,b = libs.cubes.getband(w0,w1,F[0].header)

    #Crop cube
    F[0].data = F[0].data[a:b]

    #Update header
    F[0].header["CRPIX3"] -= a

    #Get output name and save
    outFile = cubePath.replace('.fits',fileExt)
    F.writeto(outFile,overwrite=True)
    print("Saved %s."%outFile)

if __name__=="__main__":

    # Use python's argparse to handle command-line input
    parser = argparse.ArgumentParser(description='Use RA/DEC and Wavelength reference points to adjust WCS.')


    parser.add_argument('cube',
                        type=str,
                        metavar='path',
                        help='Input cube to crop.)'
    )
    parser.add_argument('wavPair',
                        type=str,
                        metavar='float tuple',
                        help='Wavelength range (in angstrom) to crop to (e.g. 4160,4180)'
    )
    parser.add_argument('-ext',
                        type=str,
                        metavar='str',
                        help='Extension to add to cropped cube filename (default: .wcrop.fits)',
                        default=".wcrop.fits"
    )
    args = parser.parse_args()

    run(args.cube,args.wavPair,fileExt=args.ext)
