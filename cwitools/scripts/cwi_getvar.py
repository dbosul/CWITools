from cwitools.analysis import estimate_variance
import argparse

def main():
    #Take any additional input params, if provided
    parser = argparse.ArgumentParser(description='Get estimated variance cube.')
    parser.add_argument('cube',
                        type=str,
                        metavar='path',
                        help='Input cube whose 3D variance you would like to estimate.'
    )
    parser.add_argument('-zWindow',
                        type=int,
                        metavar='int (px)',
                        help='Algorithm chops cube into z-bins and estimates 2D variance map at each bin by calculating it along z-axis. This parameter controls that bin size.',
                        default=10
    )
    parser.add_argument('-rescale',
                        type=str,
                        metavar='bool',
                        help="Whether or not to rescale each wavelength layer to normalize variance to sigma=1 in that layer.",
                        choices=["True","False"],
                        default="True"
    )
    parser.add_argument('-sigmaclip',
                        type=float,
                        metavar='float',
                        help="Sigma-clip threshold in stddevs to apply before estimating variance. Set to 0 to skip sigma-clipping (default: 4)",
                        default=4.0
    )
    parser.add_argument('-zmask',
                        type=str,
                        metavar='int tuple (px)',
                        help='Pair of z-indices (e.g. 21,29) to ignore (i.e. interpolate over) when calculating variance.',
                        default="0,0"
    )
    parser.add_argument('-fMin',
                        type=float,
                        metavar='float',
                        help='Minimum rescaling factor (default 0.9)',
                        default=0.9
    )
    parser.add_argument('-fMax',
                        type=float,
                        metavar='float',
                        help='Maximum rescaling factor (default 10)',
                        default=10
    )
    parser.add_argument('-ext',
                        type=str,
                        metavar='str',
                        help='Extension to add to output file (default .var.fits)',
                        default=".var.fits"
    )
    args = parser.parse_args()

    #Try to load the fits file
    try: data,header = fits.getdata(cubePath)
    except: print("Error: could not open '%s'\nExiting."%cubePath);sys.exit()

    #Try to parse the wavelength mask tuple
    try: z0,z1 = tuple(int(x) for x in zmask.split(','))
    except: print("Could not parse zmask argument. Should be two comma-separated integers (e.g. 21,32)");sys.exit()


    vardata = estimate_variance(args.cube,
        zWindow=args.zWindow,
        rescale=args.rescale,
        sigmaclip=args.sigmaclip,
        zmask=args.zmask,
        fMin=args.fMin,
        fMax=args.fMax,
    )

    varPath = cubePath.replace('.fits',fileExt)
    varFits = fits.HDUList([fits.PrimaryHDU(vardata)])
    varFits[0].header = header
    varFits.writeto(varPath,overwrite=True)
    print("Saved %s"%varPath)

if __name__=="__main__": main()