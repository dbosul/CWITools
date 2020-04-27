"""Tools for model fitting, evaluation and comparison"""
from scipy.optimize import differential_evolution
from scipy.special import voigt_profile
import numpy as np

###
### 1D MODELS in form f(params, x)
###
def gauss1d(params, x):
    """1D Gaussian profile in the form f(parameters, x)

    I(x) = I0 * exp(-(x - x0)^2 / (2 * sigma^2))

    Args:
        params (list): 1D Gaussian parameters (amplitude, mean, std_dev)
        x (numpy.array): The input  on which to evaluate the model

    Returns:
        numpy.array: The Gaussian model output

    """
    amp, mean, std = params
    return amp*np.exp( -0.5*np.power((x - mean) / std, 2))

def moffat1d(params, x):
    """1D Moffat profile in the form f(parameters, x)

    I(x) = I0 * (1 + ((x - x0)/ gamma)^2)^(-alpha)

    Args:
        params (list): 1D Moffat parameters (I0, x0, alpha, gamma)
        x (numpy.array): The input  on which to evaluate the model

    Returns:
        numpy.array: The Moffat model output

    """
    I0, x0, alpha, gamma = params
    return I0 * np.power(1 + np.power((x - x0) / gamma, 2), -alpha)

def voigt1d(params, x):
    """1D Voigt profile in the form f(parameters, x)

    From SciPy documentation:
    I(x) = Re[w(z)] / (sigma * sqrt(2 * pi))
    where w(z) is the Faddeeva function and z = (x + i * y) / (sqrt(2) * sigma)

    Args:
        params (list): 1D Voigt parameters (amplitude, sigma, gamma)
        x (numpy.array): The input  on which to evaluate the model

    Returns:
        numpy.array: The Voigt profile output

    """
    amplitude, sigma, gamma = params
    return amplitude * voigt_profile(x, sigma, gamma)

def sersic1d(params, r):
    """1D Sersic profile in the form f(parameters, r)

    I(r) = I0 * exp(-b_n * (r / Re)^(1 / n))
    where b_n is approximated as (2 * n - 1 / 3)

    Args:
        params (list): Model parameters (I0, Re, n)
        r (numpy.array): Model input

    Returns:
        numpy.array: The Sersic model
    """
    I0, re, n = params
    bn = 2 * n - 1 / 3 #Approximate b_n
    return I0 * np.exp(-bn * (np.power(r / re, 1.0 / n) - 1))

def exp1d(params, r):
    """1D Exponential profile in the form f(parameters, r)

    I(r) = I0 * exp(-beta * r/Re)

    Args:
        params (list): Model parameters (I0, Re, beta)
        x (numpy.array): Model input

    Returns:
        numpy.array: The Exponential model
    """
    I0,re,beta = params
    return I0 * np.exp(-beta * r / re )

def powlaw1d(params,r):
    """1D Power-law profile in the form f(parameters, r)

    I(R) = I0 * (R/Re) ** alpha

    Args:
        params (list): Model parameters (I0, Re, alpha)
        x (numpy.array): Model input

    Returns:
        numpy.array: The Power-law model

    Examples:


    """
    c, re, alpha = params
    return c*((r/re)**alpha)

###
### 2D MODELS in form f(params, x)
###
def gauss2d(params, xx, yy):
    """General 2D Gaussian profile in the form f(parameters, x)


    I(x) = I0 * exp(-a(x-x0)^2 - b(x-x0)(y-y0) - c(y-y0)^2)
    where
    a = cos^2 (theta) / 2*sig_x^2 + sin^2 (theta) / 2*sig_y^2
    b = sin(2*theta) / 2*sig_x^2 - sin(2*theta) / 2*sig_y^2
    c = sin^2(theta) / 2*sig_x^2 + cos^2 (theta) / 2*sig_y^2

    See gauss2d_sym for symmetric 2D Gaussian.

    Args:
        params (list): 2D Gaussian parameters (I0, x0, y0, sig_x, sig_y, theta)
            I0 - amplitude
            x0, y0 - x and y means
            sig_x, sig_y - x and y standard deviations
            theta - angle of rotation of 2D Gaussian (degrees)
        xx (numpy.ndarray): 2D meshgrid of x position
        yy (numpy.ndarray): 2D Meshgrid of y position

    Returns:
        numpy.ndarray: The 2D Gaussian model output

    """
    I0, x0, y0, sig_x, sig_y, theta = params

    #Repeated calculations
    t_rad = theta * np.pi / 180
    cos2_t = np.cos(t_rad)**2
    sin2_t = np.sin(t_rad)**2
    two_sig2_x = 2 * sig_x**2
    two_sig2_y = 2 * sig_y**2

    #Intermediate terms
    a = cos2_t / two_sig2_x + sin2_t / two_sig2_y
    b = np.sin(2 * t_rad) * (1 / two_sig2_x - 1 / two_sig2_y)
    c = sin2_t / two_sig2_x + cos2_t / two_sig2_y

    return I0 * np.exp(-a * (xx - x0)**2 - b * (xx - x0)(yy - y0) - c * (yy - y0)**2)

def gauss2d_sym(params, xx, yy):
    """Symmetric 2D Gaussian profile in the form f(parameters, x)

    I(x) = I0 * exp(-[(x-x0)^2 + (y-y0)^2] / (2*sig^2) )

    Args:
        params (list): 2D Gaussian parameters (I0, x0, y0, sig_x, sig_y, theta)
            I0 - amplitude
            x0, y0 - x and y means
            sig - standard deviation
        xx (numpy.ndarray): 2D meshgrid of x position
        yy (numpy.ndarray): 2D Meshgrid of y position

    Returns:
        numpy.ndarray: The 2D Gaussian model output

    """
    I0, x0, y0, sig = params
    return I0 * np.exp(-((xx - x0)**2 + (yy - y0)**2) / (2 * sig**2))

def moffat2d(params, xx, yy):
    """2D Moffat profile in the form f(parameters, x)

    I(x) = I0 * (1 + ((x - x0)^2 + (y-y0)^2) / gamma^2 )^(-alpha)

    Args:
        params (list): 1D Moffat parameters (I0, x0, y0, alpha, gamma)
        xx (numpy.ndarray): Meshgrid of x position
        yy (numpy.ndarray): Meshgrid of y position

    Returns:
        numpy.ndarray: The Moffat model output

    """
    I0, x0, y0, alpha, gamma = params
    return I0 * np.power(1 + ((xx - x0)**2 + (yy-y0)**2) / gamma**2, -alpha)

###
### MODEL FITTING
###
def fit_model1d(model_func, model_bounds, x, y):
    """Wrapper for fitting a 1D model using SciPy's differential evolution.


    Args:
        model_func (callable): The model function, of form f(parameters, x)
        model_bounds (list): List of tuples representing (lower, upper) bounds
            on the model parameters. e.g. [(0,1), (-1,-1), ... ]
        x (numpy.array): Input x data (e.g. wavelength)
        y (numpy.array): Input y data to fit to (e.g. flux)

    Returns:
        scipy.optimize.OptimizeResult: The result of the fit.

    """
    fit = differential_evolution(
            rss_func,
            model_bounds,
            args=(xdata, ydata, model_func)
    )
    return fit


def rss_func1d(model_params, model_func, x, y):
    """Calculate the residual sum of squares for a 1D model + 1D data.

    Args:
        model_params (list): The parameters of the model
        model_func (callable): The model function, of form f(params, x)
        x (numpy.array): The observed x-axis positions
        y (numpy.array): The observed y-axis values to fit to

    Returns:
        float: The residual sum of squares

    """
    return np.sum(np.power((y - model_func(model_params, x)), 2))

def fit_model2d(model_func, model_bounds, xx, yy, zz):
    """Fit a Gaussian or Moffat PSF


    Args:
        model_func (callable): The model function, of form f(parameters, x)
        model_bounds (list): List of tuples representing (lower, upper) bounds
            on the model parameters. e.g. [(0,1), (-1,-1), ... ]
        xx (numpy.ndarray): Input x position meshgrid
        yy (numpy.ndarray): Input y position meshgrid
        zz (numpy.ndarray): Input data to fit to (e.g. flux)

    Returns:
        scipy.optimize.OptimizeResult: The result of the fit.

    """
    fit = differential_evolution(
            rss_func,
            model_bounds,
            args=(xx, yy, model_func)
    )
    return fit
    
def rss_func2d(model_params, model_func, xx, yy, zz):
    """Calculate the residual sum of squares for a 2D model + 2D data.

    Args:
        model_params (list): The parameters of the model
        model_func (callable): The model function, of form f(params, xx, yy)
        xx (numpy.array): The observed x-axis positions
        yy (numpy.array): The observed y-axis values to fit to

    Returns:
        float: The residual sum of squares

    """
    return np.sum(np.power((zz - model_func(model_params, x)), 2))

def rss(data, model):
    """Get the residual sum of squares for a model and data.

    Args:
        data (NumPy.ndarray): Observed data.
        model (NumPy.ndarray): Modeled data.

    Returns:
        rss (float): Sum of square residuals for the input data and model.

    """
    return np.sum(np.power(data-model, 2))

###
### MODEL COMPARISON
###
def bic(model, data, k):
        """Calculate the Bayesian Information Criterion for a model.

        Args:
            model (NumPy.ndarray): The model data being evaluated
            data (NumPy.ndarray): The data being modeled
            k (int): The number of parameters in the model

        Returns:
            bic (float): The Bayesian Information Criterion

        """
        n = model.size
        rss_in = rss(data, model)
        return n * np.log(rss_in / n) + k * np.log(n)

def aic(model, data, k):
    """Calculate the Akaike Information Criterion for a model.

    Args:
        model (NumPy.ndarray): The model data being evaluated
        data (NumPy.ndarray): The data being modeled
        k (int): The number of parameters in the model

    Returns:
        aic (float): The Akaike Information Criterion

    """
    n = model.size
    rss_in = rss(data, model)
    aic0 = 2 * k + n * np.log(rss_in)

    #Correction term for small samples
    #corr -> 0 as n -> inf
    corr = (2 * k * k + 2 * k) / (n - k - 1)

    return aic0 + corr

def bic_weights(bic_list):
    """Get weights representing relative likelihood of models based on BICs.

    Args:
        bic_list (array-like): Array of BIC values for models.

    Returns:
        weights (Numpy.array): Array of relative likelihoods for models.

    """
    if type(bic_list) is list:
        bic_list = np.array(bic_list)
    delta_i = bic_list - np.min(bic_list) #Minimum AIC value of set
    rel_L = np.exp(-0.5 * delta_i) #Proportional likelihood term
    weights = rel_L / np.sum(rel_L)
    return weights


###
### MISCELLANEOUS
###
def fwhm2sigma(fwhm):
    """Convert Gaussian FWHM (Full-Width at Half Maximum) to standard deviation.

    Args:
        fwhm (float): Full-Width at Half Maximum of a Gaussian function.

    Returns:
        float: The standard deviation of the equivalent Gaussian.

    """
    return fwhm / (2 * np.sqrt(2 * np.log(2)))

def sigma2fwhm(sigma):
    """Convert standard deviation to FWHM (Full-Width at Half Maximum).

    Args:
        sigma (float): Standard deviation of a Gaussian function.

    Returns:
        float: The full-width at half-maximum of the same Gaussian function

    """
    return sigma * 2 * np.sqrt(2 * np.log(2))
