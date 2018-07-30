#!/usr/bin/env python
#
# Continuum Library - Methods for modeling & subtracting continuum emission in cubes
# 

from astropy.modeling import fitting,models
from astropy.modeling.models import custom_model
from scipy.ndimage.interpolation import shift
from scipy.optimize import least_squares

import numpy as np
import params #custom
import scipy
import sys
import warnings

import matplotlib.pyplot as plt

lines = [1216]
skylines = [[4353,4364],[4040,4050]]

def psfSubtract(fits,pos,redshift=None,vwindow=3000,radius=5,mode='scale2D',errLimit=3,inst='PCWI',k=10):
    global lines,skylines

    ##### EXTRACT DATA FROM FITS   
    data = fits[0].data         #data cube
    head = fits[0].header       #header
    
    #ROTATE (TEMPORARILY) SO THAT AXIS 2 IS 'IN-SLICE' for KCWI DATA
    if inst=='KCWI':
        data_rot = np.zeros( (data.shape[0],data.shape[2],data.shape[1]) )
        for wi in range(len(data)): data_rot[wi] = np.rot90( data[wi], k=3 )
        data = data_rot    
        pos = (data.shape[2]-pos[1],pos[0])
    
    
    w,y,x = data.shape          #Cube dimensions
    X = np.arange(x)            #Create domains X,Y and W
    Y = np.arange(y)    
    W = np.array([ head["CRVAL3"] + head["CD3_3"]*(i - head["CRPIX3"]) for i in range(w)])
    
    ##### CREATE USEFUl VARIABLES & DATA STRUCTURES 
    cmodel = np.zeros_like(data)            #Cube to store 3D continuum model   
    usewav = np.ones_like(W,dtype=bool)     #Boolean array for whether or not to use wavelengths in fitting
    Xs = np.linspace(X[0],X[-1],10*x)       #Smooth X-Y domains for PSF modelling
    Ys = np.linspace(Y[0],Y[-1],10*y)   
    ydist = 3600*np.sqrt( np.cos(head["CRVAL2"]*np.pi/180)*head["CD1_2"]**2 + head["CD2_2"]**2 ) #X & Y pixel sizes in arcseconds
    xdist = 3600*np.sqrt( np.cos(head["CRVAL2"]*np.pi/180)*head["CD1_1"]**2 + head["CD2_1"]**2 )  
    
    ry = int(round(radius/ydist))           #X and Y 'radius' extent in pixels 
    rx = int(round(radius/xdist))

    #Swap the distances if rotated for KCWI
    if inst=='KCWI':
        temp = rx
        rx = ry
        ry = temp
    ##### EXCLUDE EMISSION LINE WAVELENGTHS
    usewav[ W < head["WAVGOOD0"] ] = 0
    usewav[ W > head["WAVGOOD1"] ] = 0  
    if redshift!=None:
        for line in lines:    
            wc = (redshift+1)*line
            dw =(vwindow*1e5/3e10)*wc
            print wc-dw,wc+dw
            a,b = params.getband(wc-dw,wc+dw,head) 
            usewav[a:b] = 0
            
    #Mask bright sky lines
    for skyline in skylines:
        wC,wD = skyline
        c,d  = params.getband(wC,wD,head)
        if 0<c<W[-1] and 0<d<W[-1]: usewav[c:d] = 0        
        
    ##### OPTIMIZE CENTROID
    
    xc,yc = pos #Take input position tuple 
    
    x0,x1 = max(0,xc-rx),min(x,xc+rx+1) #Get bounding box for PSF fit
    y0,y1 = max(0,yc-ry),min(y,yc+ry+1)
 
    img = np.sum(data[usewav,y0:y1,x0:x1],axis=0) #Create white light image
    
    xdomain,xdata = range(x1-x0), np.sum(img,axis=0) #Get X and Y PSF profiles/domains
    ydomain,ydata = range(y1-y0), np.sum(img,axis=1)
    
    fit = fitting.SimplexLSQFitter() #Get astropy fitter class

    moffat_bounds = {'amplitude':(0,float("inf")) }
    xMoffInit = models.Moffat1D(max(xdata),x_0=xc-x0,bounds=moffat_bounds) #Initial guesses
    yMoffInit = models.Moffat1D(max(ydata),x_0=yc-y0,bounds=moffat_bounds)
    
    xMoffFit = fit(xMoffInit,xdomain,xdata) #Fit Moffat1Ds to each axis
    yMoffFit = fit(yMoffInit,ydomain,ydata)
    
    xc_new = xMoffFit.x_0.value + x0
    yc_new = yMoffFit.x_0.value + y0

    
    #If the new centroid is beyond our anticipated error range away... just use scale method
    if abs(xc-xc_new)*xdist>errLimit or abs(yc-yc_new)*ydist>errLimit: mode='scale2D'
                    
    #Otherwise, update the box to center better on our continuum source
    else:
        xc, yc = int(round(xc_new)),int(round(yc_new)) #Round to nearest integer
        x0,x1 = max(0,xc-rx),min(x,xc+rx+1) #Get new ranges
        y0,y1 = max(0,yc-ry),min(y,yc+ry+1)

        xc = max(0,min(x-1,xc)) #Bound new variables to within image
        yc = max(0,min(y-1,yc)) 

    #This method creates a 2D continuum image and scales it at each wavelength.
    if mode=='scale2D':
    
        print 'scale2D',
        
        ##### CREATE CROPPED CUBE     
        cube = data[:,y0:y1,x0:x1].copy()              #Create smaller working cube to isolate continuum source

        ##### CREATE 2D CONTINUUM IMAGE           
        cont2d = np.mean(cube[usewav],axis=0) #Create 2D continuum image
        cont2d -= np.median(cont2d) #Median subtract image (to get zero background level)
        
        fitter = fitting.LinearLSQFitter()
        
        ##### BUILD 3D CONTINUUM MODEL       
        for i in range(cube.shape[0]):

            scale_init = models.Scale()
            scale_fit = fitter(scale_init,np.ndarray.flatten(cont2d),np.ndarray.flatten(cube[i]-np.median(cube[i])))
            model = scale_fit.factor.value*cont2d #Add this wavelength layer to the model
            
            if 0 and 4403.5<W[i]<4409.5:
                plt.figure(figsize=(18,6))
                plt.subplot(131)
                plt.pcolor(cube[i],vmin=np.min(cube[i]),vmax=np.max(cube[i]))
                plt.subplot(132)
                plt.pcolor(model,vmin=np.min(cube[i]),vmax=np.max(cube[i]))
                plt.subplot(133)
                plt.pcolor(cube[i]-model,vmin=np.min(cube[i]),vmax=np.max(cube[i]))
                plt.show()
            

            data[i,y0:y1,x0:x1] -= model #Subtract from data cube
        
            cmodel[i,y0:y1,x0:x1] += model #Add to larger model cube

    #This method just fits a simple line to the spectrum each spaxel; for flat continuum sources.
    elif mode=='lineFit':
        
        print 'lineFit',
        #Define custom astropy model class (just a line)
        @custom_model
        def line(xx,m=0,c=0): return m*xx + c
           
        #Run through pixels in 2D region
        for yi in range(y0,y1):
            for xi in range(x0,x1):
                              
                m_init = line() #Create initial guess model
                m = fit(m_init, W[usewav], data[usewav,yi,xi]) #Optimize model
                
                model = m(W)
                
                cmodel[:,yi,xi] += model
                data[:,yi,xi] -= model
        
    #This method extracts a central spectrum and fits it to each spaxel
    elif mode=='specFit':
    
        print 'specFit',
        #Define custom astropy model class (just a line)
        @custom_model
        def line(xx,m=0,c=0): return m*xx + c

        #Optimizer and model for residuals 
        residual_fitter = fitting.LinearLSQFitter()
        residual_model = models.Polynomial1D(degree=k) 
    
        ##### GET QSO SPECTRUM
        q_spec = data[:,yc,xc].copy()
        q_spec_fit = q_spec[usewav==1]

        #Run through slices
        for yi in range(y0,y1):
        
            print yi,
            sys.stdout.flush()
            
            #If this not the main QSO slice
            if yi!=yc:
                            
                #Extract QSO spectrum for this slice
                s_spec = data[:,yi,xc].copy() 
                s_spec_fit = s_spec[usewav==1]

                #Estimate wavelength shift needed
                corr = scipy.signal.correlate(s_spec,q_spec)
                corrs = scipy.ndimage.filters.gaussian_filter1d(corr,5.0)
                w_offset = 0#(np.nanargmax(corrs)-len(corrs)/2)/2.0

                #Find wavelength offset (px) for this slice
                chisq = lambda x: s_spec_fit[10:-10] - x[0]*scipy.ndimage.interpolation.shift(q_spec_fit,x[1],order=0,mode='reflect')[10:-10]

                p0 = [np.max(s_spec)/np.max(q_spec),w_offset]
                
                lbound = [0.0,-5]
                ubound = [5.1, 5]        
                for j in range(len(p0)):
                    if p0[j]<lbound[j]: p0[j]=lbound[j]
                    elif p0[j]>ubound[j]: p0[j]=ubound[j]
                
                p_fit = scipy.optimize.least_squares(chisq,p0,bounds=(lbound,ubound),jac='3-point')                

                A0,dw0 =p_fit.x
                
            else:
                A0 = 0.5
                dw0=0
                
            lbound = [0.0,-5]
            ubound = [20.0,5]
            
            for xi in range(x0,x1):

                spec = data[:,yi,xi]
                spec_fit = spec[usewav==1]
                             
                #First fit to find wav offset for this slice
                chisq = lambda x: spec_fit - x[0]*scipy.ndimage.interpolation.shift(q_spec_fit,x[1],order=0,mode='reflect') 

                p0 = [A0,dw0]
                for j in range(len(p0)):
                    if p0[j]<lbound[j]: p0[j]=lbound[j]
                    elif p0[j]>ubound[j]: p0[j]=ubound[j]
                    #elif abs(p0[j]<1e-6): p0[j]=0

                sys.stdout.flush()
                p_fit = scipy.optimize.least_squares(chisq,p0,bounds=(lbound,ubound),jac='3-point')
                p_fit = scipy.optimize.least_squares(chisq,p_fit.x,bounds=(lbound,ubound),jac='3-point')

                A,dw = p_fit.x                
                m_spec = A*scipy.ndimage.interpolation.shift(q_spec,0,order=0,mode='reflect') 
                
                #Do a linear fit to residual and correct linear errors
                residual = data[:,yi,xi]-m_spec
                
                ydata = residual[usewav==1]
                xdata = W[usewav==1]

                res_mod = residual_fitter(residual_model,xdata,ydata)
                res_fit = res_mod(W)

                
                model =  m_spec + res_fit             
                residual2 = data[:,yi,xi] - model

                if 1 and abs(yi-yc)<3 and abs(xi-xc)<3:

                    plt.figure(figsize=(16,8))
                    
                    plt.subplot(311)
                    plt.title(r"$A=%.4f,d\lambda=%.3fpx$" % (A,dw))
                    plt.plot(W[usewav==0],spec[usewav==0],'kx')
                    plt.plot(W[usewav],spec[usewav],'k.')
                    plt.plot(W,model,'r-')
                    plt.xlim([W[0],W[-1]])
                    plt.ylim([1.5*min(spec),max(spec)*1.5])
                    plt.subplot(312)
                    plt.xlim([W[0],W[-1]])

                    plt.plot(W[usewav==0],residual[usewav==0],'gx')
                    plt.plot(W[usewav],residual[usewav],'g.')
                    plt.plot(W,res_fit,'k-')
              
                    plt.ylim([1.5*min(residual),max(spec)*1.5])  
                                                      
                    plt.subplot(313)
                    #plt.hist(residual)
                    plt.plot(W[usewav==0],residual2[usewav==0],'bx')
                    plt.plot(W[usewav],residual2[usewav],'b.')
                    plt.xlim([W[0],W[-1]])
                    plt.tight_layout()           
                    plt.show()
                            
                            
                cmodel[:,yi,xi] += model
                data[:,yi,xi] -= model
     
    elif mode=='scale1D':
    
        print 'scale1D',

        for yi in range(y0,y1):

            print yi,
            sys.stdout.flush()
            
            ##### CREATE CROPPED CUBE 

            slice2D = data[:,yi,:].copy()  #Create smaller working cube to isolate continuum source

            XX = np.arange(x0,x1)
            ##### CREATE 2D CONTINUUM IMAGE           
            slice1D = np.mean(slice2D[usewav],axis=0) #Create 2D continuum image
            
            ###Try to fit a moffat to the Slice1D
            
            fitter = fitting.LinearLSQFitter()
            
            ##### BUILD 3D CONTINUUM MODEL       
            for i in range(slice2D.shape[0]):
               
                #try:
            
                A0 = max(0,float(np.sum(slice2D[i]))/np.sum(slice1D)) #Initial guess for scaling factor
                
                scale_init = models.Scale()

                useX = np.ones_like(X,dtype='bool')
                useX[X<xc-2] = 0
                useX[X>xc+2] = 0
                scale_fit = fitter(scale_init,slice1D[useX],slice2D[i][useX])

                model = scale_fit.factor.value*slice1D #Add this wavelength layer to the model

                if scale_fit.factor.value<=0: continue
                
                if 0 and 4610<W[i]<4620:
                    plt.figure(figsize=(18,9))
                    plt.subplot(211)
                    plt.plot(X,slice2D[i],'kx')
                    plt.plot(X[useX],slice2D[i][useX],'ko')
                    plt.plot(X,model,'r-')
                    plt.subplot(212)
                    plt.plot(X,slice2D[i]-model,'bx')
                    plt.show()
                
                data[i,yi,:] -= model #Subtract from data cube
            
                cmodel[i,yi,:] += model #Add to larger model cube
                    
                    

                #except:
                #    print "ERROR: Slice %i, Wav %i: unable to fit. Doing dumb subtraction." % (yi,i)
                #    model = A0*slice1D #Add this wavelength layer to the model

                #    data[i,yi,x0:x1] -= model #Subtract from data cube
                
                #    cmodel[i,yi,x0:x1] += model #Add to larger model cube         
        
    #ROTATE BACK IF ROTATED AT START
    if inst=='KCWI':
        data_rot = np.zeros( (data.shape[0],data.shape[2],data.shape[1]) )
        cmodel_rot = np.zeros( (data.shape[0],data.shape[2],data.shape[1]) )
        for wi in range(len(data)):
            data_rot[wi] = np.rot90( data[wi], k=1 )
            cmodel_rot[wi] = np.rot90( cmodel[wi], k=1 )
        data = data_rot
        cmodel = cmodel_rot
        
    return data,cmodel  
        
        
        
#Return a 3D cube which is a simple 1D polynomial fit to each 2D spaxel            
def polyModel(cube,usewav,k=5,inst='PCWI'):

    print "\tPolyFit to masked cube. Slice:",
        
    #Useful data structures
    w,y,x = cube.shape
    model = np.zeros_like(cube)
    W = np.arange(w)

    if not usewav.any():
        print "\t No useable wavelengths indicated for polyfit routine. Returning empty model."
        return model
            
    #Optimizer and model
    fitter = fitting.LinearLSQFitter()
    p = models.Polynomial1D(degree=k) #Initialize model
        
    if inst=='PCWI':

        #Run through spaxels and fit
        for yi in range(y):
            print yi,
            sys.stdout.flush()
            for xi in range(x):        
                p = fitter(p,W[usewav],cube[usewav,yi,xi])     
                model[:,yi,xi] = p(W[:])
                
    elif inst=='KCWI':

        #Run through spaxels and fit
        for xi in range(x):
            print xi,
            sys.stdout.flush()
            for yi in range(y):        
                p = fitter(p,W[usewav],cube[usewav,yi,xi])     
                model[:,yi,xi] = p(W[:])    
    
    else: print "Instrument not recognized: %s" % inst    
    print ""
    
    #Return model
    return model

#Return a 3D cube which is a simple 1D polynomial fit to each 2D spaxel            
def polySliceModel(cube,usewav,k=5,inst='PCWI'):

    print "\tPolyFit to masked cube. Slice:",
        
    #Useful data structures
    w,y,x = cube.shape
    model = np.zeros_like(cube)
    W = np.arange(w)

    if not usewav.any():
        print "\t No useable wavelengths indicated for polyfit routine. Returning empty model."
        return model
            
    #Optimizer and model
    fitter = fitting.LinearLSQFitter()
    p = models.Polynomial1D(degree=k) #Initialize model
        
    if inst=='PCWI':

        #Run through spaxels and fit
        for yi in range(y):
            print yi,
            sys.stdout.flush()
            p = fitter(p,W[usewav],np.mean(cube[usewav,yi,:],axis=1))
            for xi in range(x): model[:,yi,xi] = p(W[:])
                
    elif inst=='KCWI':

        #Run through slices and fit
        for xi in range(x):
            print xi,
            sys.stdout.flush()
            p = fitter(p,W[usewav],np.mean(cube[usewav,:,xi],axis=1))     
            for yi in range(y): model[:,yi,xi] = p(W[:])    
    
    else: print "Instrument not recognized: %s" % inst    
    print ""
    
    #Return model
    return model         
            
            
            

    

        
