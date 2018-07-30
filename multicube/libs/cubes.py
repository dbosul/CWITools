#!/usr/bin/env python
#
# Cubes Library - Methods for manipulating 3D FITS cubes (masking, aligning, coadding etc)
# 

from astropy.modeling import models,fitting
from astropy.wcs import WCS
from scipy.ndimage.interpolation import shift

import numpy as np

import qso

  
def fixWCS(fits_list,params):
    
    #Run through each fits image
    for i,fits in enumerate(fits_list):
    
        #First, get accurate in-cube X,Y location of QSO
        plot_title = "Select the object at RA:%.4f DEC:%.4f" % (params["RA"],params["DEC"])
        qfinder = qso.qsoFinder(fits,params["Z"],title=plot_title)
        x,y = qfinder.run()
        
        #Update parameters with new X,Y location
        params["SRC_X"][i] = x
        params["SRC_Y"][i] = y

        #Insert param-based RA/DEC into header
        h = fits[0].header
        if "RA" in h["CTYPE1"] and "DEC" in h["CTYPE2"]:
                 
            fits[0].header["CRVAL1"] = params["RA"]
            fits[0].header["CRVAL2"] = params["DEC"]
            
            fits[0].header["CRPIX1"] = x
            fits[0].header["CRPIX2"] = y
            
        elif "DEC" in h["CTYPE1"] and "RA" in h["CTYPE2"]:
        
            fits[0].header["CRVAL1"] = params["DEC"]
            fits[0].header["CRVAL2"] = params["RA"]
            
            fits[0].header["CRPIX1"] = y
            fits[0].header["CRPIX2"] = x        
        
        else:
        
            print "%s - RA/DEC not aligned with X/Y axes. WCS correction for this orientation is not yet implemented." % params["IMG_ID"][i]
        

    return fits_list
      
#######################################################################
#Take rotated, stacked images, use center of QSO to align
def wcsAlign(fits_list,params):


    print("Aligning modified cubes using QSO centers")
    
    good_fits,xpos,ypos = [],[],[]
    
    #Calculate positions of QSOs in cropped, rotated, scaled images
    x,y = [],[]
             
    xpos = np.array([f[0].header["CRPIX1"] - f[0].data.shape[2]/2 for f in fits_list])
    ypos = np.array([f[0].header["CRPIX2"] - f[0].data.shape[1]/2 for f in fits_list])
     

    #Calculate offsets from first image
    dx = xpos - xpos[0]
    dy = ypos - ypos[0] 
    
    #Get max size of any image in X and Y dimensions
    cube_shapes = np.array( [ f[0].data.shape for f in fits_list ] )
    Xmax,Ymax = np.max(cube_shapes[:,2]),np.max(cube_shapes[:,1])

    #Get maximum shifts needed in either direction
    dx_max = np.max(np.abs(dx))
    dy_max = np.max(np.abs(dy))
    
    #Create max canvas size needed for later stacking
    Y,X = int(round(Ymax + 2*dy_max + 2)), int(round(Xmax + 2*dx_max + 2))

    for i,fits in enumerate(fits_list):

        #Extract shape and imgnum info
        w,y,x = fits[0].data.shape
        
        #Get padding required to initially center data on canvas
        xpad,ypad = int((X-x)/2), int((Y-y)/2)

        #Create new cube, fill in data and apply shifts
        new_cube = np.zeros( (w,Y,X) )
        new_cube[:,ypad:ypad+y,xpad:xpad+x] = np.copy(fits[0].data)
        
        #Update reference pixel after padding
        fits[0].header["CRPIX1"]  += xpad
        fits[0].header["CRPIX2"]  += ypad
        
        #Using linear interpolation, shift image by sub-pixel values
        new_cube = shift(new_cube,(0,-dy[i],-dx[i]),order=1)
        
        #Edges will now be blurred a bit due to interpolation. Trim these edge artefacts
        y0,y1 = ypad - int(dy[i]), ypad + y - int(dy[i])
        x0,x1 = xpad - int(dx[i]), xpad + x - int(dx[i])
        new_cube[:,y0-1:y0+1,:] = 0
        new_cube[:,y1-1:y1+1,:] = 0
        new_cube[:,:,x0-1:x0+1] = 0
        new_cube[:,:,x1-1:x1+1] = 0
        
        #Update header after shifting
        fits[0].header["CRPIX1"]  -= dx[i]
        fits[0].header["CRPIX2"]  -= dy[i]
        
        #Update data in FITS image
        fits[0].data = np.copy(new_cube)
    
        
    return fits_list
#######################################################################

#######################################################################
#Take rotated, stacked images, use center of QSO to align
def coadd(fits_list,params):

    vartime = True
    
    print("Coadding aligned cubes.")
    
    #Create empty stack and exposure mask for coadd
    w,y,x = fits_list[0][0].data.shape
    
    stack = np.zeros((w,y,x))
    exp_mask = np.zeros((y,x))

    header = fits_list[0][0].header

    #Create Stacked cube and fill out mask of exposure times
    for i,fits in enumerate(fits_list):
    
        if params["INST"][i]=="PCWI": exptime = fits[0].header["EXPTIME"]
        elif params["INST"][i]=="KCWI": exptime = fits[0].header["TELAPSE"]
        else:
            print("Bad instrument parameter - %s" % params["INST"][i])
            raise Exception

        if header["BUNIT"] == "FLAM":
            img = np.sum(fits[0].data,axis=0)
            img[img!=0] = exptime
            exp_mask += img
            if vartime == False:
                stack += exptime*fits[0].data
            else:
                stack += exptime**2*fits[0].data
           
    #Divide each spaxel by the exposure count
    for yi in range(y):
        for xi in range(x):
            E = exp_mask[yi,xi]            
            if E>0:
                if header["BUNIT"] == "FLAM":
                    if vartime == False:
                        stack[:,yi,xi] /= E
                    else:
                        stack[:,yi,xi] /= E**2
                    

    stack_img = np.sum(stack,axis=0)
    
    #Trim off 0/nan edges from grid
    trim_mode = "nantrim"
    if trim_mode=="nantrim": 
        y1,y2,x1,x2 = 0,y-1,0,x-1
        while np.sum(stack_img[y1])==0: y1+=1
        while np.sum(stack_img[y2])==0: y2-=1
        while np.sum(stack_img[:,x1])==0: x1+=1
        while np.sum(stack_img[:,x2])==0: x2-=1
    elif trim_mode=="overlap":
        expmax = np.max(exp_mask)
        y1,y2,x1,x2 = 0,y-1,0,x-1
        while np.max(exp_mask[y1])<expmax: y1+=1
        while np.max(exp_mask[y2])<expmax: y2-=1
        while np.max(exp_mask[:,x1])<expmax: x1+=1
        while np.max(exp_mask[:,x2])<expmax: x2-=1        

    #Crop stacked cube
    stack = stack[:,y1:y2,x1:x2]

    #Update header after cropping
    header["CRPIX1"] -= x1
    header["CRPIX2"] -= y1
    
    return stack,header

def get_mask(fits,regfile):

    print "\tGenerating 2D mask from region file"
        
    #EXTRACT/CREATE USEFUL VARS############
    data3D = fits[0].data
    head3D = fits[0].header

    W,Y,X = data3D.shape #Dimensions
    mask = np.zeros((Y,X),dtype=int) #Mask to be filled in
    x,y = np.arange(X),np.arange(Y) #Create X/Y image coordinate domains
    xx, yy = np.meshgrid(x, y) #Create meshgrid of X, Y
    ww = np.array([ head3D["CRVAL3"] + head3D["CD3_3"]*(i - head3D["CRPIX3"]) for i in range(W)])
    
    yPS = np.sqrt( np.cos(head3D["CRVAL2"]*np.pi/180)*head3D["CD1_2"]**2 + head3D["CD2_2"]**2 ) #X & Y plate scales (deg/px)
    xPS = np.sqrt( np.cos(head3D["CRVAL2"]*np.pi/180)*head3D["CD1_1"]**2 + head3D["CD2_1"]**2 )
        
    fit = fitting.SimplexLSQFitter() #Get astropy fitter class
    Lfit = fitting.LinearLSQFitter() 
    
    usewav = np.ones_like(ww,dtype=bool)
    usewav[ww<head3D["WAVGOOD0"]] = 0
    usewav[ww>head3D["WAVGOOD1"]] = 0
    
    data2D = np.sum(data3D[usewav],axis=0)
    med = np.median(data2D)

    #BUILD MASK############################
    if regfile[0].coord_format=='image':

        rr = np.sqrt( (xx-x0)**2 + (yy-y0)**2 )
        mask[rr<=R] = i+1          
                    
    elif regfile[0].coord_format=='fk5':  
    
        #AIC = 2k + n Log(RSS/n) [ - (2k**2 +2k )/(n-k-1) ]
        def AICc(dat,mod,k):
            RSS = np.sum( (dat-mod)**2 )
            n = np.size(dat)
            return 2*k + n*np.log(RSS/n) #+ (2*k**2 + 2*k)/(n-k-1)
            
        head2D = head3D.copy() #Create a 2D header by modifying 3D header
        for key in ["NAXIS3","CRPIX3","CD3_3","CRVAL3","CTYPE3","CNAME3","CUNIT3"]: head2D.remove(key)
        head2D["NAXIS"]=2
        head2D["WCSDIM"]=2
        wcs = WCS(head2D)    
        ra, dec = wcs.wcs_pix2world(xx, yy, 0) #Get meshes of RA/DEC
        
        for i,reg in enumerate(regfile):    
        
            ra0,dec0,R = reg.coord_list #Extract location and default radius    
            rr = np.sqrt( (np.cos(dec*np.pi/180)*(ra-ra0))**2 + (dec-dec0)**2 ) #Create meshgrid of distance to source 
            
            if np.min(rr) > R: continue #Skip any sources more than one default radius outside the FOV
            
            else:
                
                yc,xc = np.where( rr == np.min(rr) ) #Take input position tuple 
                xc,yc = xc[0],yc[0]
                
                rx = 2*int(round(R/xPS)) #Convert angular radius to distance in pixels
                ry = 2*int(round(R/yPS))

                x0,x1 = max(0,xc-rx),min(X,xc+rx+1) #Get bounding box for PSF fit
                y0,y1 = max(0,yc-ry),min(Y,yc+ry+1)

                img = np.mean(data3D[usewav,y0:y1,x0:x1],axis=0) #Not strictly a white-light image
                img -= np.median(img) #Correct in case of bad sky subtraction
                
                xdomain,xdata = range(x1-x0), np.mean(img,axis=0) #Get X and Y domains/data
                ydomain,ydata = range(y1-y0), np.mean(img,axis=1)
                
                moffat_bounds = {'amplitude':(0,float("inf")) }
                xMoffInit = models.Moffat1D(max(xdata),x_0=xc-x0,bounds=moffat_bounds) #Initial guess Moffat profiles
                yMoffInit = models.Moffat1D(max(ydata),x_0=yc-y0,bounds=moffat_bounds)
                xLineInit = models.Linear1D(slope=0,intercept=np.mean(xdata))
                yLineInit = models.Linear1D(slope=0,intercept=np.mean(ydata))
                
                xMoffFit = fit(xMoffInit,xdomain,xdata) #Fit Moffat1Ds to each axis
                yMoffFit = fit(yMoffInit,ydomain,ydata)
                xLineFit = Lfit(xLineInit,xdomain,xdata) #Fit Linear1Ds to each axis
                yLineFit = Lfit(yLineInit,ydomain,ydata)
                
                kMoff = len(xMoffFit.parameters) #Get number of parameters in each model
                kLine = len(xLineFit.parameters)
                
                xMoffAICc = AICc(xdata,xMoffFit(xdomain),kMoff) #Get Akaike Information Criterion for each
                xLineAICc = AICc(xdata,xLineFit(xdomain),kLine)
                yMoffAICc = AICc(ydata,yMoffFit(ydomain),kMoff)
                yLineAICc = AICc(ydata,yLineFit(ydomain),kLine)
                
                xIsMoff = xMoffAICc < xLineAICc # Determine if Moffat is a better fit than a simple line
                yIsMoff = yMoffAICc < yLineAICc
                
                if xIsMoff and yIsMoff: #If source has detectable moffat profile (i.e. bright source) expand mask

                    xfwhm = xMoffFit.gamma.value*2*np.sqrt(2**(1/xMoffFit.alpha.value) - 1) #Get FWHMs
                    yfwhm = yMoffFit.gamma.value*2*np.sqrt(2**(1/yMoffFit.alpha.value) - 1)

                    R = 2*max(xfwhm*xPS,yfwhm*yPS)
                
                mask[rr < R] = i+1

    return mask
    
def apply_mask(cube,mask,mode='zero',inst='PCWI'):

    print "\tApplying mode=%s filter under mask." % mode
    
    if mode=='zero':
        
        #Just replace with zeros
        for wi in range(cube.shape[0]): cube[wi][mask>0] = 0
    
    elif mode=='cubemedian':
    
        #Replace with cube-wide median
        cubemed = np.median(cube)
        for wi in range(cube.shape[0]): cube[wi][mask>0] = cubemed
    
    elif mode=='xmedian':
        
        if inst=='PCWI':
            
            for yi in range(cube.shape[1]):
            
                #Get 1D median wavelength profile of slice
                slicemedprof = np.median(cube[:,yi,:],axis=1)     
                
                #Apply to spaxels that are masked  
                for xi in range(cube.shape[2]):
                    
                    if mask[yi,xi] > 0: cube[:,yi,xi] = slicemedprof
                    
        elif inst=='KCWI':
        
            for xi in range(cube.shape[2]):    
            
                #Get 1D median wavelength profile of slice
                slicemedprof = np.median(cube[:,:,xi],axis=1)     
                
                #Apply to spaxels that are masked  
                for yi in range(cube.shape[1]):
                    
                    if mask[yi,xi] > 0: cube[:,yi,xi] = slicemedprof
                                    
                
            
    else: print "Apply_Mask: Mode not recognized."
    
    return cube
    
           
