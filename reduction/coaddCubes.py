"""Stack input cubes into a master frame using a CWITools parameter file.
"""

from .. import libs

from astropy.io import fits as fitsIO

import argparse
import numpy as np
import sys
import time



def run(paramPath,cubeType,pxThresh=None,expThresh=None,propVar=False,plot=False,pa=0):

    #Timer start
    tStart = time.time()

    #Try to load the param file
    try: params = libs.params.loadparams(paramPath)
    except: print("Error: could not open '%s'\nExiting."%paramPath);sys.exit()

    propVar=True if propVar.upper()=="TRUE" else False
    plot=True if plot.upper()=="TRUE" else False

    #Check if parameters are complete
    libs.params.verify(params)

    #Add file extension of omitted
    if not ".fits" in cubeType: cubeType += ".fits"

    #Get filenames
    files = libs.io.findfiles(params,cubeType)

    #Stack cubes and trim
    stackedFITS,varFITS = libs.cubes.coadd(files,params,expThresh=expThresh,pxThresh=pxThresh,propVar=propVar,PA=pa,plot=plot)

    #Add redshift info to header
    stackedFITS[0].header["Z"] = params["Z"]
    stackedFITS[0].header["ZLA"] = params["ZLA"]

    #Save stacked cube
    stackedpath = '%s%s_%s' % (params["PRODUCT_DIR"],params["NAME"],cubeType)
    stackedFITS[0].writeto(stackedpath,overwrite=True)
    print("\nSaved %s" % stackedpath)

    #Save variance cube if one was returned
    if varFITS!=None:
        varpath = '%s%s_%s' % (params["PRODUCT_DIR"],params["NAME"],cubeType.replace("icube","vcube"))
        varFITS[0].writeto(varpath,overwrite=True)
        print("Saved %s" % varpath)

    #Timer end
    tFinish = time.time()
    print("Elapsed time: %.2f seconds" % (tFinish-tStart))

if __name__=="__main__":

    # Use python's argparse to handle command-line input
    parser = argparse.ArgumentParser(description='Coadd data cubes.')

    mainGroup = parser.add_argument_group(title="Main",description="Basic input")
    mainGroup.add_argument('paramFile',
                        type=str,
                        metavar='Parameter File',
                        help='CWITools Parameter file (used to load cube list etc.)'
    )
    mainGroup.add_argument('cubeType',
                        type=str,
                        metavar='Cube Type',
                        help='The type of cube (i.e. file extension such as \'icubed.fits\') to coadd'
    )

    methodGroup = parser.add_argument_group(title="Methods",description="Parameters related to coadd methods.")
    methodGroup.add_argument('-pxThresh',
                        type=float,
                        metavar='Pixel Threshold',
                        help='Fraction of a coadd-frame pixel that must be covered by an input frame to be included (0-1)',
                        default=0.5
    )
    methodGroup.add_argument('-expThresh',
                        type=float,
                        metavar='Exposure Threshold',
                        help='Crop cube to include only spaxels with this fraction of the maximum overlap (0-1)',
                        default=0.75
    )
    methodGroup.add_argument('-pa',
                        type=float,
                        metavar='float (deg)',
                        help='Position Angle of output frame.',
                        default=0
    )
    fileIOGroup = parser.add_argument_group(title="Input/Output",description="File input/output options.")
    fileIOGroup.add_argument('-propVar',
                        type=str,
                        metavar='bool',
                        help='Propagate error through coadd process (i.e. coadd the variance cubes also.)',
                        choices=["True","False"],
                        default="False"
    )
    fileIOGroup.add_argument('-plot',
                        type=str,
                        metavar='bool',
                        help='Display plots of on-sky footprints (warning: slows down code a bit.)',
                        choices=["True","False"],
                        default="False"
    )
    args = parser.parse_args()

    args.plot = (args.plot.upper()=="TRUE")
    args.propVar = (args.propVar.upper()=="TRUE")

    run(args.paramFile, args.cubeType,
        pxThresh=args.pxThresh,
        expThresh=args.expThresh,
        pa=args.pa,
        propVar = args.propVar,
        plot=args.plot
    )
