from .. imports libs

from astropy.io import fits as fitsIO

import argparse
import numpy as np
import sys
import matplotlib.pyplot as plt

def run(paramPath,cubeType,fixRADEC=True,fixWav=False,skyLine=None,RA=None,
        DEC=None,simpleMode=False):
    """Corrects the world-coordinate system of data cubes using interactive tools.

    Args:
        paramPath (str): Path to the CWITools parameter file.
        cubeType (str): Type of cube to work with (e.g. icubes.fits)
        fixRADEC (bool): Fix the spatial axes (Default: True)
        fixWav (bool): Fix the wavelength axis (Default: True)
        skyLine (float): Known wavelength of a fittable sky-line.
            This parameter is required for fixing the wavelength solution.
        RA (float): RA (dd.dd) of source used to correct WCS (overrides param file)
        DEC (float): DEC (dd.dd) of source used to correct WCS (overrides param file)
        simpleMode (bool): Set to True to only correct the given filetype.
            By default, CWITools will also apply corrections to the associated
            variance (vcube), sky (scube) and object (ocube) files.
            
    """

    #Make sure sky line is provided if running wav correction
    if fixWav and skyLine is None: print("No sky emission line provided. Will load defaults from the line-lists in CWITools/data/sky/.")

    #Try to load the param file
    try: params = libs.params.loadparams(paramPath)
    except: print("Error: could not open '%s'\nExiting."%paramPath);sys.exit()

    if RA is None: RA = params["RA"]
    if DEC is None: DEC = params["DEC"]

    # Get regular and sky filenames
    files = libs.io.findfiles(params,cubeType)

    # Get regular and sky filenames
    fits = [ fitsIO.open(f) for f in files ]

    # Get Nod-and-Shuffle status of each fits (based on paramfile)
    nas = []
    for i,f in enumerate(fits):
        if params["IMG_ID"][i]==params["SKY_ID"][i] or params["INST"][i]=="KCWI": nas.append(True)
        else: nas.append(False)

    #Get length before any sky files are added
    N = len(fits)
    inst = [ x for x in params["INST"] ]

    #Get cubetype without the file extensions ('icube', 'scube', 'ocube')
    cubetypeShort = cubeType.split(".")[0][:-1]

    # Get any sky images that are needed (for fixwav)
    if fixWav:

        if not np.array(nas).all() and not (np.array(params["INST"])=="KCWI").all():

            #Add image numbers and instrument names to lists
            snums,sinst = [],[]
            for i,s in enumerate(params["SKY_ID"]):
                if s not in params["IMG_ID"] and s not in snums:
                    snums.append(s)
                    sinst.append(params["INST"][i])

            #Create file paths
            sfiles = [ files[0].replace(params["IMG_ID"][0],s) for s in snums ]

            #Load fits files
            sfits  = [ fitsIO.open(s) for s in sfiles ]

            #Update relevant lists
            for i in range(len(sfiles)):

                files.append(sfiles[i])
                fits.append(sfits[i])
                inst.append(sinst[i])
                nas.append(False)


    #Run through all images now and perform corrections
    for i,fileName in enumerate(files):

        #Get current CD matrix
        crval1,crval2,crval3 = ( fits[i][0].header["CRVAL%i"%(k+1)] for k in range(3) )
        crpix1,crpix2,crpix3 = ( fits[i][0].header["CRPIX%i"%(k+1)] for k in range(3) )

        #Get RA/DEC values if fixWAV requested
        if fixRADEC:

            if nas[i]:
                radecFile = fileName if simpleMode else fileName.replace(cubetypeShort,'icube')
                radecFITS = fitsIO.open(radecFile)
            else: radecFITS = fits[i]


            #If this is a target image, we can use target to correct RA/DEC
            if i<len(params["IMG_ID"]):

                #Measure RA/DEC center values for this exposure
                crval1,crval2,crpix1,crpix2 = libs.cubes.fixRADEC(radecFITS,RA,DEC)

            else: crval1,crval2,crpix1,crpix2 = ( radecFITS[0].header[k] for k in ["CRVAL1","CRVAL2","CRPIX1","CRPIX2"] )

            radecFITS.close()

        #Get wavelength WCS values if fixWav requested
        if fixWav:

            if nas[i]:
                skyFile   = fileName.replace(cubeType,'scube.fits')
                skyFITS   = fitsIO.open(skyFile)
            else: skyFITS = fits[i]

            crval3,crpix3 = libs.cubes.fixWav(skyFITS,inst[i],skyLine=skyLine)

            skyFITS.close()

        #Create lists of crval/crpix values, whether updated or not
        crvals = [ crval1, crval2, crval3 ]
        crpixs = [ crpix1, crpix2, crpix3 ]


        if simpleMode:

            #Try to load, but continue upon failure
            try: f = fitsIO.open(fileName)
            except:
                print("Could not open %s. Cube will not be corrected." % filePath)
                continue

            #Fix each of the header values
            for k in range(3):

                f[0].header["CRVAL%i"%(k+1)] = crvals[k]
                f[0].header["CRPIX%i"%(k+1)] = crpixs[k]

            #Save WCS corrected cube
            wcPath = fileName.replace('.fits','.wc.fits')
            f[0].writeto(wcPath,overwrite=True)
            print("Saved %s"%wcPath)

        else:

            #Make list of relevant cubes to be corrected - default is always icube and vcube type files
            cubes = ['icube','vcube']

            #For NAS or KCWI data - also include the scube and ocube files
            if nas[i] or inst[i]=="KCWI":
                cubes.append('scube')
                cubes.append('ocube')

            #Load fits, modify header and save for each cube type
            for c in cubes:

                #Get filepath for this cube
                filePath = fileName.replace(cubetypeShort,c)

                #Try to load, but continue upon failure
                try: f = fitsIO.open(filePath)
                except:
                    print("Could not open %s. Cube will not be corrected." % filePath)
                    continue

                #Fix each of the header values
                for k in range(3):

                    f[0].header["CRVAL%i"%(k+1)] = crvals[k]
                    f[0].header["CRPIX%i"%(k+1)] = crpixs[k]

                #Save WCS corrected cube
                wcPath = filePath.replace('.fits','.wc.fits')
                f[0].writeto(wcPath,overwrite=True)
                print("Saved %s"%wcPath)

#If executed on the command line:
if __name__=="__main__":

    # Use python's argparse to handle command-line input
    parser = argparse.ArgumentParser(description='Use RA/DEC and Wavelength reference points to adjust WCS.')


    parser.add_argument('paramFile',
                        type=str,
                        metavar='str',
                        help='CWITools Parameter file (used to load cube list etc.)'
    )
    parser.add_argument('cubeType',
                        type=str,
                        metavar='str',
                        help='The type of cube (i.e. file extension such as \'icubed.fits\') to coadd'
    )
    parser.add_argument('-fixWav',
                        type=str,
                        metavar='boolean',
                        help='Set to True/False to turn Wavelength correction on/off',
                        choices=["True","False"],
                        default="True"
    )
    parser.add_argument('-skyLine',
                        type=float,
                        metavar='float',
                        help='Wavelength of sky line to use for correcting WCS. (angstrom)'
    )
    parser.add_argument('-fixRADEC',
                        type=str,
                        metavar='boolean',
                        help='Set to True/False to turn RA/DEC correction on/off',
                        choices=["True","False"],
                        default="True"
    )
    parser.add_argument('-simpleMode',
                        type=str,
                        metavar='boolean',
                        help='Simple mode. By default, fixWCS will use scube products for fixWav, icube products for fixRADEC,\
                         and try to apply those fixes to all relevant cube types. Set -simple to True to force fixWCS to work only with the given filetype. ',
                        choices=["True","False"],
                        default="False"
    )
    parser.add_argument('-ra',
                        type=float,
                        metavar='float (deg)',
                        help='RA of source you are using for this - if not the same as parameter file target',
    )
    parser.add_argument('-dec',
                        type=float,
                        metavar='float (deg)',
                        help='DEC of source you are using for this - if not the same as parameter file target',
    )
    args = parser.parse_args()

    #Parse str boolean flags to bool types
    args.fixWav = True if args.fixWav=="True" else False
    args.fixRADEC = True if args.fixRADEC=="True" else False
    args.simpleMode = True if args.simpleMode=="True" else False

    run(args.paramFile,args.cubeType,
        fixRADEC=args.fixRADEC,
        fixWav=args.fixWav,
        skyLine=args.skyLine,
        RA=args.ra,
        DEC=args.dec,
        simpleMode=args.simpleMode
    )
