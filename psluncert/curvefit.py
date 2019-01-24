''' Calculations for finding uncertainty in fitted curves.

The CurveFit class computes fit function and uncertainty for any
arbitrary curve function.

CurveFitParam objects specify a single coefficient from a CurveFit as the value
of interest to pull, for example the slope value, into an UncertCalc instance.
'''

import inspect
import numpy as np
import sympy
from scipy import odr
import scipy.optimize
from scipy import interpolate
import yaml

from . import uncertainty
from . import out_curvefit
from . import out_uncert
from . import uarray
from . import uparser
from .uarray import Array  # Explicitly import Array so it can be accessed via curvefit.Array


class CurveFit(object):
    ''' Fitting an arbitrary function curve to measured data points and computing
        uncertainty in the fit parameters.

        Parameters
        ----------
        arr: Array object
            The array of data points to operate on

        func: string or callable
            Function to fit data to. For common functions, give a string name of the function,
            one of: (line, quad, cubic, poly, exp, decay, log, logistic). A 'poly' must also provide
            the polyorder argument.

            Any other string will be evaluated as an expression, and must contain an 'x' variable.

            If func is callable, arguments must take the form func(x, *args) where x is array
            of independent variable and *args are parameters of the fit. For example a quadratic
            fit using a lambda function:
            lambda x, a, b, c: return a + b*x + c*x**2

        name: string
            Name of the function

        desc: string
            Description of the function

        polyorder: int
            Order for polynomial fit. Only required for fit == 'poly'.

        p0: list, optional
            Initial guess for function parameters

        method: string, optional
            Method passed to scipy.curve_fit

        bounds: 2-tuple, optional
            Upper and Lower bounds for fit parameters, passed to scipy.curve_fit. If specified,
            will also set priors for MCMC method to uniform between bounds. (Ignored with ODR
            fitting method).

        odr: bool
            Force use of orthogonal regression

        xdates: bool
            Reports should interpret x values as dates. Note array x values must be floats,
            ie datetime.toordinal() values.

        absolute_sigma: boolean
            Treat uncertainties in an absolute sense. If false, only relative
            magnitudes matter.

        Notes
        -----
        Uses scipy.optimize.curve_fit or scipy.odr to do the fitting, depending on
        if the array has uncertainty in x (or if odr parameter is True). p0 is required if ODR is used.

        Saving and loading to config file is only supported when func is given as a string.
    '''
    def __init__(self, arr, func='line', name='curvefit', desc='', polyorder=None, p0=None, method=None,
                 bounds=None, odr=None, seed=None, xdates=False, absolute_sigma=True):
        self.seed = seed
        self.samples = 5000
        self.outputs = {}
        self.arr = arr
        self.name = name
        self.desc = desc
        self.out = None
        self.xname = 'x'
        self.yname = 'y'
        self.xdates = xdates
        self.absolute_sigma = absolute_sigma
        self.set_fitfunc(func, polyorder=polyorder, p0=p0, method=method, bounds=bounds, odr=odr)

    def set_fitfunc(self, func, polyorder=2, method=None, bounds=None, odr=None, p0=None):
        ''' Set up fit function '''
        self.fitname = func
        self.polyorder = polyorder
        self.odr = odr
        self.bounds = bounds
        self.p0 = p0

        if callable(func):
            self.fitname = 'callable'

        elif self.fitname == 'line':
            self.expr = sympy.sympify('a + b*x')
            def func(x, b, a):
                return a + b*x

        elif self.fitname == 'exp':  # Full exponential
            self.expr = sympy.sympify('c + a * exp(x/b)')
            def func(x, a, b, c):
                return c + a * np.exp(x/b)

        elif self.fitname == 'decay':  # Exponential decay to zero (no c parameter)
            self.expr = sympy.sympify('a * exp(-x/b)')
            def func(x, a, b):
                return a * np.exp(-x/b)

        elif self.fitname == 'decay2':  # Exponential decay, using rate lambda rather than time constant tau
            self.expr = sympy.sympify('a * exp(-x*b)')
            def func(x, a, b):
                return a * np.exp(-x*b)

        elif self.fitname == 'log':
            self.expr = sympy.sympify('a + b * log(x-c)')
            def func(x, a, b, c):
                return a + b * np.log(x-c)

        elif self.fitname == 'logistic':
            self.expr = sympy.sympify('a / (1 + exp((x-c)/b)) + d')
            def func(x, a, b, c, d):
                return d + a / (1 + np.exp((x-c)/b))

        elif self.fitname == 'quad' or (func == 'poly' and polyorder == 2):
            self.expr = sympy.sympify('a + b*x + c*x**2')
            def func(x, a, b, c):
                return a + b*x + c*x*x

        elif self.fitname == 'cubic' or (func == 'poly' and polyorder == 3):
            self.expr = sympy.sympify('a + b*x + c*x**2 + d*x**3')
            def func(x, a, b, c, d):
                return a + b*x + c*x*x + d*x*x*x

        elif self.fitname == 'poly':
            def func(x, *p):
                return np.poly1d(p)(x)

            polyorder = int(polyorder)
            if polyorder < 1 or polyorder > 12:
                raise ValueError('Polynomial order out of range')
            varnames = [chr(ord('a')+i) for i in range(polyorder+1)]
            self.expr = sympy.sympify('+'.join([v+'*x**{}'.format(i) for i, v in enumerate(varnames)]))

            # variable *args must have initial guess for scipy
            if self.p0 is None:
                self.p0 = np.ones(polyorder+1)
        else:
            # actual expression as string
            func, self.expr, _ = self.check_expr(self.fitname)

        self.func = func

        if self.fitname == 'poly' and polyorder > 3:
            # poly def above doesn't have named arguments, so the inspect won't find them. Name them here.
            self.pnames = varnames
        else:
            self.pnames = list(inspect.signature(self.func).parameters.keys())[1:]
        self.numparams = len(self.pnames)

        if self.fitname == 'callable':
            self.expr = sympy.sympify('f(x, ' + ', '.join(self.pnames) + ')')

        if self.bounds is None:
            bounds = (-np.inf, np.inf)
        else:
            bounds = self.bounds
            self.set_mcmc_priors([lambda x, a=blow, b=bhi: (x > a) & (x <= b) for blow, bhi in zip(bounds[0], bounds[1])])

        if self.fitname == 'line' and not odr:
            # use generic LINE fit for lines with no odr
            self.fitfunc = lambda x, y, ux, uy, absolute_sigma=self.absolute_sigma: genlinefit(x, y, ux, uy, absolute_sigma=absolute_sigma)
        else:
            self.fitfunc = lambda x, y, ux, uy, absolute_sigma=self.absolute_sigma: genfit(self.func, x, y, ux, uy, p0=self.p0, method=method, bounds=bounds, odr=odr, absolute_sigma=absolute_sigma)

        return self.expr

    @classmethod
    def check_expr(cls, expr):
        ''' Check expr string for a valid curvefit function including an x variable
            and at least one fit parameter.

            Returns
            -------
            func: callable
                Lambdified function of expr
            symexpr: sympy object
                Sympy expression of function
            argnames: list of strings
                Names of arguments (except x) to function
        '''
        uparser.check_expr(expr)  # Will raise if not valid expression
        symexpr = sympy.sympify(expr)
        argnames = sorted([str(s) for s in symexpr.free_symbols])
        if 'x' not in argnames:
            raise ValueError('Expression must contain "x" variable.')
        argnames.remove('x')
        if len(argnames) == 0:
            raise ValueError('Expression must contain one or more parameters to fit.')
        func = sympy.lambdify(['x'] + argnames, symexpr, 'numpy')  # Make sure to specify 'numpy' so nans are returned instead of complex numbers
        return func, symexpr, argnames

    def clear(self):
        ''' Clear the sampled points '''
        self.arr.clear()
        self.outputs = {}

    def get_output(self):
        ''' Get output object '''
        return self.out

    def run_uyestimate(self):
        if (self.arr.uy_estimate is None and (not self.arr.has_ux() and not self.arr.has_uy())):
            # Estimate uncertainty using residuals if uy not provided. LSQ method does this already,
            # do the same for GUM and MC.
            self.arr.uy_estimate = self.estimate_uy()

    def calculate(self, **kwargs):
        ''' Calculate curve fit by different methods and display the results.
            Only least-squares analytical method is calculated by default.

            Keyword Arguments
            -----------------
            lsq: bool
                Calculate analytical Least Squares method
            gum: bool
                Calculate GUM method
            mc: bool
                Calculate Monte Carlo method
            mcmc: bool
                Calculate Markov-Chain Monte Carlo method

            samples: int
                Number of Monte Carlo samples

            Returns
            -------
            FuncOutput object
        '''
        self.run_uyestimate()
        samples = kwargs.get('samples', self.samples)
        outputs = []
        if kwargs.get('lsq', True):
            outputs.append(self.calc_LSQ())
        if kwargs.get('gum', False):
            outputs.append(self.calc_GUM())
        if kwargs.get('mc', False):
            outputs.append(self.calc_MC(samples=samples))
        if kwargs.get('mcmc', False):
            outputs.append(self.calc_MCMC(samples=samples, burnin=kwargs.get('burnin', .2)))
        self.out = out_curvefit.CurveFitOutput(outputs, self, xdates=self.xdates)
        return self.out

    def calc_LSQ(self):
        ''' Calculate analytical Least-Squares curve fit and uncertainty.

            Returns
            -------
            CurveFitOutput object
        '''
        uy = np.zeros(len(self.arr.x)) if not self.arr.has_uy() else self.arr.uy
        coeff, cov = self.fitfunc(self.arr.x, self.arr.y, self.arr.ux, uy)

        resids = (self.arr.y - self.func(self.arr.x, *coeff))  # All residuals (NOT squared)
        sigmas = np.sqrt(np.diag(cov))
        degf = len(self.arr.x) - len(coeff)
        if self.absolute_sigma or not self.arr.has_uy():
            w = np.full(len(self.arr.x), 1)  # Unweighted residuals in Syx
        else:
            w = (1/uy**2)  # Determine weighted Syx
            w = w/sum(w) * len(self.arr.y)      # Normalize weights so sum(wi) = N
        SSres = sum(w*resids**2)   # Sum-of-squares of residuals
        Syx = np.sqrt(SSres/degf)  # Standard error of the estimate (based on residuals)
        cor = cov / sigmas[:, None] / sigmas[None, :]  # See numpy code for corrcoeff
        SSreg = sum(w*(self.func(self.arr.x, *coeff) - sum(w*self.arr.y)/sum(w))**2)
        r = np.sqrt(1-SSres/(SSres+SSreg))
        params = {
            'coeffs': coeff,
            'sigmas': sigmas,
            'mean': coeff,
            'uncert': sigmas,
            'resids': resids,
            'Syx': Syx,
            'r': r,
            'F': SSreg * degf / SSres,
            'SSres': SSres,
            'SSreg': SSreg,
            'func': self.func,
            'fitname': self.fitname,
            'expr': self.expr,
            'pnames': self.pnames,
            'cov': cov,
            'cor': cor,
            'degf': degf,
            'data': (self.arr.x, self.arr.y, self.arr.ux, self.arr.uy),
            'u_conf': lambda x, coeff=coeff, cov=cov, func=self.func: _get_uconf(x, coeff, cov, func),
            'u_pred': lambda x, mode='Syx', coeff=coeff, cov=cov, func=self.func, Syx=Syx, sigy=self.arr.uy, xdata=self.arr.x: _get_upred(x, coeff, cov, func, Syx, sigy, xdata=xdata, mode=mode),
            'axnames': (self.xname, self.yname),
            'has_uy': self.arr.has_uy(),
            'xdates': self.xdates
            }
        self.outputs['lsq'] = out_curvefit.create_output_curvefit('lsq', **params)
        return self.outputs['lsq']

    def sample(self, samples=1000):
        ''' Generate Monte Carlo samples '''
        self.arr.clear()
        self.arr.sample(samples)

    def estimate_uy(self):
        ''' Calculate an estimate for uy using residuals of fit for when uy is not given.
            This is what linefit() method does behind the scenes, this function allows the
            same behavior for GUM and Monte Carlo.
        '''
        pcoeff, _ = self.fitfunc(self.arr.x, self.arr.y, None, None)
        uy = np.sqrt(np.sum((self.func(self.arr.x, *pcoeff) - self.arr.y)**2)/(len(self.arr.x) - len(pcoeff)))
        uy = np.full(len(self.arr.x), uy)
        return uy

    def calc_MC(self, samples=1000, sensitivity=False):
        ''' Calculate Monte Carlo curve fit and uncertainty.

            Parameters
            ----------
            samples: int
                Number of Monte Carlo samples

            Returns
            -------
            CurveFitOutput object
        '''
        self.run_uyestimate()
        uy = self.arr.uy if self.arr.uy_estimate is None else self.arr.uy_estimate
        if self.arr.xsamples is None or self.arr.ysamples is None or self.arr.xsamples.shape[1] != samples:
            self.sample(samples)

        self.samplecoeffs = np.zeros((samples, self.numparams))
        for i in range(samples):
            self.samplecoeffs[i], _ = self.fitfunc(self.arr.xsamples[:, i], self.arr.ysamples[:, i], None, None)

        coeff = self.samplecoeffs.mean(axis=0)
        sigma = self.samplecoeffs.std(axis=0, ddof=1)

        resids = (self.arr.y - self.func(self.arr.x, *coeff))
        degf = len(self.arr.x) - len(coeff)
        if self.absolute_sigma or not self.arr.has_uy():
            w = np.full(len(self.arr.x), 1)  # Unweighted residuals in Syx
        else:
            w = (1/uy**2)  # Determine weighted Syx
            w = w/sum(w) * len(self.arr.y)   # Normalize weights so sum(wi) = N
        y_vs_x_sample = lambda x, i: self.func(x, *self.samplecoeffs[i])
        cov = np.cov(self.samplecoeffs.T)
        cor = np.corrcoef(self.samplecoeffs.T)
        SSres = sum(w*resids**2)   # Sum-of-squares of residuals
        Syx = np.sqrt(SSres/degf)  # Standard error of the estimate (based on residuals)
        SSreg = sum(w*(self.func(self.arr.x, *coeff) - sum(w*self.arr.y)/sum(w))**2)
        r = np.sqrt(1-SSres/(SSres+SSreg))
        u_conf = lambda x: np.std(np.array([y_vs_x_sample(x, i) for i in range(samples)]), axis=0, ddof=1)
        u_pred = lambda x, mode='Syx', Syx=Syx, sigy=self.arr.uy, u_conf=u_conf, xdata=self.arr.x: _get_upred_MC(x, u_conf=u_conf, Syx=Syx, sigy=sigy, xdata=xdata, mode=mode)
        params = {
            'coeffs': coeff,
            'sigmas': sigma,
            'mean': coeff,
            'uncert': sigma,
            'func': self.func,
            'expr': self.expr,
            'fitname': self.fitname,
            'resids': resids,
            'Syx': Syx,
            'r': r,
            'F': SSreg * degf / SSres,
            'samples': self.samplecoeffs,
            'y_vs_x_sample': y_vs_x_sample,
            'u_conf': u_conf,
            'u_pred': u_pred,
            'pnames': self.pnames,
            'cov': cov,
            'cor': cor,
            'degf': degf,
            'data': (self.arr.x, self.arr.y, self.arr.ux, uy),
            'axnames': (self.xname, self.yname),
            'has_uy': self.arr.has_uy(),
            'xdates': self.xdates
            }
        self.outputs['mc'] = out_curvefit.create_output_curvefit('mc', **params)
        return self.outputs['mc']

    def calc_MCMC(self, samples=10000, burnin=0.2):
        ''' Calculate Markov-Chain Monte Carlo (Metropolis-in-Gibbs algorithm)
            fit parameters and uncertainty

            Parameters
            ----------
            samples: int
                Total number of samples to generate
            burnin: float
                Fraction of samples to reject at start of chain

            Returns
            -------
            CurveFitOutput object

            Notes
            -----
            Currently only supported with constant u(y) and u(x) = 0.
        '''
        if self.seed is not None:
            np.random.seed(self.seed)

        self.run_uyestimate()
        uy = self.arr.uy if self.arr.uy_estimate is None else self.arr.uy_estimate

        if self.arr.has_ux():
            print('WARNING - MCMC algorithm ignores u(x) != 0')
        if np.max(uy) != np.min(uy):
            print('WARNING - MCMC algorithm with non-constant u(y). Using mean.')

        # Find initial guess/sigmas
        p, cov = self.fitfunc(self.arr.x, self.arr.y, self.arr.ux, uy)
        up = np.sqrt(np.diag(cov))
        if not all(np.isfinite(up)):
            raise ValueError('MCMC Could not determine initial sigmas. Try providing p0.')

        if all(uy == 0):
            # Sigma2 is unknown. Estimate from residuals and vary through trace.
            resids = (self.arr.y - self.func(self.arr.x, *p))
            sig2 = resids.var(ddof=1)
            sresid = np.std(np.array([self.arr.y, self.func(self.arr.x, *p)]), axis=0)
            sig2sig = 2*np.sqrt(sig2)
            sig2lim = np.percentile(sresid, 5)**2, np.percentile(sresid, 95)**2
            varysigma = True
        else:
            # Sigma2 (variance of data) is known. Use it and don't vary sigma during trace.
            sig2 = uy.mean()**2
            varysigma = False

        if not hasattr(self, 'priors') or self.priors is None:
            priors = [lambda x: 1 for i in range(len(self.pnames))]
        else:
            priors = [p if p is not None else lambda x: 1 for p in self.priors]

        for pidx in range(len(p)):
            if priors[pidx](p[pidx]) <= 0:
                # Will get div/0 below
                raise ValueError('Initial prior for parameter {} is < 0'.format(self.pnames[pidx]))

        accepts = np.zeros(len(p))
        self.mcmccoeffs = np.zeros((samples, self.numparams))
        self.sig2trace = np.zeros(samples)
        for i in range(samples):
            for pidx in range(len(p)):
                pnew = p.copy()
                pnew[pidx] = pnew[pidx] + np.random.normal(scale=up[pidx])

                Y = self.func(self.arr.x, *p)  # Value using p (without sigma that was appended to p)
                Ynew = self.func(self.arr.x, *pnew)  # Value using pnew

                # NOTE: could use logpdf, but it seems slower than manually writing it out:
                # problog = stat.norm.logpdf(self.arr.y, loc=I, scale=np.sqrt(sig2).sum()
                problog = -1/(2*sig2) * sum((self.arr.y - Y)**2)
                problognew = -1/(2*sig2) * sum((self.arr.y - Ynew)**2)

                r = np.exp(problognew-problog) * priors[pidx](pnew[pidx]) / priors[pidx](p[pidx])
                if r >= np.random.uniform():
                    p = pnew
                    accepts[pidx] += 1

            if varysigma:
                sig2new = sig2 + np.random.normal(scale=sig2sig)
                if (sig2new < sig2lim[1] and sig2new > sig2lim[0]):
                    Y = self.func(self.arr.x, *p)
                    ss2 = sum((self.arr.y - Y)**2)
                    problog = -1/(2*sig2) * ss2
                    problognew = -1/(2*sig2new) * ss2
                    if np.exp(problognew - problog) >= np.random.uniform():
                        sig2 = sig2new

            self.mcmccoeffs[i, :] = p
            self.sig2trace[i] = sig2
        burnin = int(burnin * samples)
        self.mcmccoeffs = self.mcmccoeffs[burnin:, :]
        self.sig2trace = self.sig2trace[burnin:]

        coeff = self.mcmccoeffs.mean(axis=0)
        sigma = self.mcmccoeffs.std(axis=0, ddof=1)
        resids = (self.arr.y - self.func(self.arr.x, *coeff))
        degf = len(self.arr.x) - len(coeff)
        if self.absolute_sigma or not self.arr.has_uy():
            w = np.full(len(self.arr.x), 1)  # Unweighted residuals in Syx
        else:
            w = (1/uy**2)  # Determine weighted Syx
            w = w/sum(w) * len(self.arr.y)   # Normalize weights so sum(wi) = N
        y_vs_x_sample = lambda x, i, v=self.mcmccoeffs: self.func(x, *v[i, :])
        cov = np.cov(self.mcmccoeffs.T)
        cor = np.corrcoef(self.mcmccoeffs.T)
        SSres = sum(w*resids**2)   # Sum-of-squares of residuals
        Syx = np.sqrt(SSres/degf)  # Standard error of the estimate (based on residuals)
        SSreg = sum(w*(self.func(self.arr.x, *coeff) - sum(w*self.arr.y)/sum(w))**2)
        r = np.sqrt(1-SSres/(SSres+SSreg))
        u_conf = lambda x: np.std(np.array([y_vs_x_sample(x, i) for i in range(len(self.mcmccoeffs))]), axis=0, ddof=1)
        u_pred = lambda x, mode='Syx', Syx=Syx, sigy=self.arr.uy, u_conf=u_conf, xdata=self.arr.x: _get_upred_MC(x, u_conf=u_conf, Syx=Syx, sigy=sigy, xdata=xdata, mode=mode)

        params = {
            'coeffs': coeff,
            'sigmas': sigma,
            'mean': coeff,
            'uncert': sigma,
            'acceptance': accepts/samples,
            'func': self.func,
            'fitname': self.fitname,
            'expr': self.expr,
            'resids': resids,
            'Syx': Syx,
            'r': r,
            'F': SSreg * degf / SSres,
            'samples': self.mcmccoeffs,
            'y_vs_x_sample': y_vs_x_sample,
            'u_conf': u_conf,
            'u_pred': u_pred,
            'pnames': self.pnames,
            'cov': cov,
            'cor': cor,
            'degf': degf,
            'data': (self.arr.x, self.arr.y, self.arr.ux, uy),
            'axnames': (self.xname, self.yname),
            'has_uy': self.arr.has_uy(),
            'xdates': self.xdates
            }
        self.outputs['mcmc'] = out_curvefit.create_output_curvefit('mcmc', **params)
        return self.outputs['mcmc']

    def set_mcmc_priors(self, priors):
        ''' Set prior distribution functions for each input to be used in
            Markov-Chain Monte Carlo.

            Parameters
            ----------
            priors: list of callables
                List of functions, one for each fitting parameter. Each function must
                take a possible fit parameter as input and return the probability
                of that parameter from 0-1.

            Notes
            -----
            If set_mcmc_priors is not called, all priors will return 1.
        '''
        assert len(priors) == len(self.pnames)
        self.priors = priors

    def calc_GUM(self, correlation=None):
        ''' Calculate curve fit and uncertainty using GUM Approximation.

            Parameters
            ----------
            correlation: array, optional
                Correlation matrix

            Returns
            -------
            output: CurveFitOutput object
        '''
        self.run_uyestimate()
        uy = self.arr.uy if self.arr.uy_estimate is None else self.arr.uy_estimate

        coeff, cov, grad = uarray._GUM(lambda x, y: self.fitfunc(x, y, None, None)[0], self.arr.x, self.arr.y, self.arr.ux, uy)
        sigmas = np.sqrt(np.diag(cov))
        resids = (self.arr.y - self.func(self.arr.x, *coeff))
        degf = len(self.arr.x) - len(coeff)
        if self.absolute_sigma or not self.arr.has_uy():
            w = 1  # Unweighted residuals in Syx
        else:
            w = (1/uy**2)  # Determine weighted Syx
            w = w/sum(w) * len(self.arr.y)   # Normalize weights so sum(wi) = N
        SSres = sum(w*resids**2)
        Syx = np.sqrt(SSres/degf)  # Standard error of the estimate (based on residuals)
        cor = cov / sigmas[:, None] / sigmas[None, :]  # See numpy code for corrcoeff
        SSreg = sum(w*(self.func(self.arr.x, *coeff) - self.arr.y.mean())**2)
        r = np.sqrt(1-SSres/(SSres+SSreg))
        params = {
            'coeffs': coeff,
            'sigmas': sigmas,
            'mean': coeff,
            'uncert': sigmas,
            'func': self.func,
            'fitname': self.fitname,
            'expr': self.expr,
            'resids': resids,
            'Syx': Syx,
            'r': r,
            'F': SSreg * degf / SSres,
            'pnames': self.pnames,
            'grad': grad,
            'cov': cov,
            'cor': cor,
            'degf': degf,
            'data': (self.arr.x, self.arr.y, self.arr.ux, uy),
            'u_conf': lambda x, coeff=coeff, sigmas=sigmas, cov=cov, func=self.func: _get_uconf(x, coeff, cov, func),
            'u_pred': lambda x, mode='Syx', coeff=coeff, cov=cov, func=self.func, Syx=Syx, sigy=self.arr.uy, xdata=self.arr.x: _get_upred(x, coeff, cov, func, Syx, sigy, xdata=xdata, mode=mode),
            'axnames': (self.xname, self.yname),
            'has_uy': self.arr.has_uy(),
            'xdates': self.xdates
            }
        self.outputs['gum'] = out_curvefit.create_output_curvefit('gum', **params)
        return self.outputs['gum']

    def get_config(self):
        if self.fitname == 'callable':
            raise ValueError('Saving CurveFit only supported for line, poly, and exp fits.')

        d = {}
        d['mode'] = 'curvefit'
        d['curve'] = self.fitname
        d['name'] = self.name
        d['desc'] = self.desc
        d['odr'] = self.odr
        d['xname'] = self.xname
        d['yname'] = self.yname
        d['xdates'] = self.xdates
        d['abssigma'] = self.absolute_sigma
        if self.fitname == 'poly':
            d['order'] = self.polyorder
        if self.p0 is not None:
            d['p0'] = self.p0
        if self.bounds is not None:
            d['bound0'] = self.bounds[0]
            d['bound1'] = self.bounds[1]

        d['arrx'] = self.arr.x.astype('float').tolist()  # Can't yaml numpy arrays, use list
        d['arry'] = self.arr.y.astype('float').tolist()
        if self.arr.has_ux():
            d['arrux'] = list(self.arr.ux)
        if self.arr.has_uy():
            d['arruy'] = list(self.arr.uy)
        return d

    def save_config(self, fname):
        ''' Save configuration to file.

            Parameters
            ----------
            fname: string or file
                File name or file object to save to
        '''
        d = self.get_config()
        out = yaml.dump([d], default_flow_style=False)
        try:
            fname.write(out)
        except AttributeError:
            with open(fname, 'w') as f:
                f.write(out)

    @classmethod
    def from_config(cls, config):
        fit = config['curve']
        order = config.get('order', 2)
        name = config.get('name', None)
        desc = config.get('desc', '')
        p0 = config.get('p0', None)
        odr = config.get('odr', None)
        seed = config.get('seed', None)
        xdates = config.get('xdates', False)
        absolute_sigma = config.get('abssigma', True)
        x = np.asarray(config.get('arrx'), dtype=float)
        y = np.asarray(config.get('arry'), dtype=float)
        ux = config.get('arrux', 0.)
        uy = config.get('arruy', 0.)
        arr = Array(x, y, ux=ux, uy=uy)
        newfit = cls(arr, fit, polyorder=order, name=name, desc=desc, p0=p0, odr=odr, seed=seed, xdates=xdates, absolute_sigma=absolute_sigma)
        newfit.xname = config.get('xname', 'x')
        newfit.yname = config.get('yname', 'y')
        return newfit

    @classmethod
    def from_configfile(cls, fname):
        ''' Read and parse the configuration file. Returns a new UncertRisk
            instance.

            Parameters
            ----------
            fname: string or file
                File name or open file object to read configuration from
        '''
        try:
            try:
                yml = fname.read()  # fname is file object
            except AttributeError:
                with open(fname, 'r') as fobj:  # fname is string
                    yml = fobj.read()
        except UnicodeDecodeError:
            # file is binary, can't be read as yaml
            return None

        try:
            config = yaml.safe_load(yml)
        except yaml.scanner.ScannerError:
            return None  # Can't read YAML

        u = cls.from_config(config[0])  # config yaml is always a list
        return u


class CurveFitParam(uncertainty.InputFunc):
    ''' One parameter of curve fit. Use this class to include fitting parameters in
        a broader UncertCalc calculation (for example, take the difference
        between two linefit slopes)

        Parameters
        ----------
        ifunc: CurveFit
            The curve fitting object to extract a single parameter from

        pidx: int or float
            If mode parameter is 'param', pidx is index of the desired coefficient (e.g.
            pidx=0 for slope of linear fit). If mode parameter is 'pred', pidx is an
            x value at which to predict the y value and its uncertainty.

        mode: string
            See pidx

        name: string, optional
            Name for the function, as used in UncertCalc

        desc: string, optional
            Description for the function, as used in UncertCalc
    '''
    def __init__(self, ifunc, pidx, mode='param', name='', desc=''):
        assert isinstance(ifunc, CurveFit)
        self.pidx = pidx
        self.ifunc = ifunc
        self.mode = mode   # param or predicted
        self.name = name
        self.desc = desc
        self.uncerts = []
        self.ftype = 'array'
        self.sampledvalues = None
        self.report = True
        self.outputs = {}

    def __str__(self):
        return self.name

    def clear(self):
        ''' Clear the sampled data '''
        self.ifunc.clear()
        self.sampledvalues = None
        self.outputs = {}

    def stdunc(self):
        ''' Get standard uncertainty of the parameter or fit value '''
        if self.mode == 'param':
            if 'lsq' in self.outputs:
                return self.outputs['lsq'].uncert[0]
            elif 'mc' in self.outputs:
                return self.outputs['mc'].uncert[0]
            elif 'gum' in self.outputs:
                return self.outputs['gum'].uncert[0]
            self.calc_LSQ()
            return self.outputs['lsq'].uncert[0]
        elif self.mode == 'pred':
            if 'lsq' in self.outputs:
                return self.outputs['lsq'].u_pred(self.pidx)
            elif 'mc' in self.outputs:
                return self.outputs['mc'].u_pred(self.pidx)
            elif 'gum' in self.outputs:
                return self.outputs['gum'].u_pred(self.pidx)
            self.calc_LSQ()
            return self.outputs['lsq'].u_predy(self.pidx)
        else:
            raise

    @property
    def nom(self):
        ''' Get nominal value of parameter '''
        return self.mean()

    def mean(self):
        ''' Get mean value of the parameter or fit value '''
        if self.mode == 'param':
            if 'lsq' in self.outputs:
                return self.outputs['lsq'].mean[0]
            elif 'mc' in self.outputs:
                return self.outputs['mc'].mean[0]
            elif 'gum' in self.outputs:
                return self.outputs['gum'].mean[0]
            self.calc_LSQ()
            return self.outputs['lsq'].mean[0]
        elif self.mode == 'pred':
            if 'lsq' in self.outputs:
                return self.outputs['lsq'].y(self.pidx)
            elif 'mc' in self.outputs:
                return self.outputs['mc'].y(self.pidx)
            elif 'gum' in self.outputs:
                return self.outputs['gum'].y(self.pidx)
            self.calc_LSQ()
            return self.outputs['lsq'].y(self.pidx)

    def degf(self):
        ''' Get degrees of freedom '''
        if 'lsq' in self.outputs:
            return self.outputs['lsq'].degf
        elif 'mc' in self.outputs:
            return self.outputs['mc'].degf
        elif 'gum' in self.outputs:
            return self.outputs['gum'].degf
        self.calc_LSQ()
        return self.outputs['lsq'].degf

    def sample(self, samples=1000):
        ''' Generate Monte Carlo samples '''
        self.ifunc.sample(samples)
        self.sampledvalues = self.ifunc.samplecoeffs[:, self.pidx]
        return self.sampledvalues

    # These functions should not be called for this subclass
    def get_basefunc(self):
        raise NotImplementedError

    def get_basevars(self):
        return []

    def get_basesymbols(self):
        raise NotImplementedError

    def get_basemeans(self):
        raise NotImplementedError

    def get_baseuncerts(self):
        raise NotImplementedError

    def get_basenames(self):
        return []

    def get_latex(self):
        ''' Get LaTeX representation of this function '''
        return sympy.latex(sympy.Symbol(self.name))

    def get_symbol(self):
        ''' Get sympy representation of name '''
        return sympy.Symbol(self.name)

    def calculate(self, **kwargs):
        ''' Calculate all available methods.

            Keyword Arguments
            -----------------
            gum: bool
                Calculate GUM method
            mc: bool
                Calculate Monte Carlo method
            mcmc: bool
                Calculate Markov-Chain Monte Carlo method
            lsq: bool
                Calculate analytical Least Squares method
            samples: int
                Number of Monte Carlo samples

            Returns
            -------
            FuncOutput object
        '''
        samples = kwargs.get('samples', 5000)
        outs = []
        if kwargs.get('gum', True):
            self.outputs['gum'] = self.calc_GUM(kwargs.get('correlation'))
            outs.append(self.outputs['gum'])
        if kwargs.get('mc', True):
            self.outputs['mc'] = self.calc_MC(samples=samples)
            outs.append(self.outputs['mc'])
        if kwargs.get('mcmc', False):
            self.outputs['mcmc'] = self.calc_MCMC(samples=samples)
            outs.append(self.outputs['mc'])
        if kwargs.get('lsq', True):
            self.outputs['lsq'] = self.calc_LSQ()
            outs.append(self.outputs['lsq'])
        self.out = out_uncert.FuncOutput(outs, self)
        return self.out

    def calc_GUM(self, correlation=None):
        ''' Calculate the GUM solution

            Parameters
            ----------
            correlation: array, optional
                Correlation matrix

            Returns
            -------
            BaseOutput object
        '''
        gumout = self.ifunc.calc_GUM()
        if self.mode == 'param':
            self.outputs['gum'] = out_uncert.create_output('gum', mean=gumout.mean[self.pidx], uncert=gumout.uncert[self.pidx], **gumout.properties)
        elif self.mode == 'pred':
            self.outputs['gum'] = out_uncert.create_output('gum', mean=gumout.y(self.pidx), uncert=gumout.u_pred(self.pidx), **gumout.properties)
        return self.outputs['gum']

    def calc_MC(self, samples=1000, sensitivity=None):
        ''' Calculate Monte Carlo solution

            Parameters
            ----------
            samples: int
                Number of Monte Carlo samples
            sensitivity: bool
                Run sensitivity calculation (requires additional Monte Carlo runs)

            Returns
            -------
            BaseOutputMC object
        '''
        mcout = self.ifunc.calc_MC(samples=samples, sensitivity=sensitivity)
        samples = mcout.properties.pop('samples')[:, self.pidx]
        samples = np.atleast_2d(samples).T
        if self.mode == 'param':
            self.outputs['mc'] = out_uncert.create_output('mc', mean=mcout.mean[self.pidx], uncert=mcout.uncert[self.pidx], samples=samples, **mcout.properties)
        elif self.mode == 'pred':
            self.outputs['mc'] = out_uncert.create_output('mc', mean=mcout.y(self.pidx), uncert=mcout.u_pred(self.pidx), samples=samples, **mcout.properties)
        return self.outputs['mc']

    def calc_MCMC(self, samples=1000, sensitivity=None, **kwargs):
        ''' Calculate Markov Chain Monte Carlo solution

            Parameters
            ----------
            samples: int
                Number of Monte Carlo samples (including burnin)
            sensitivity: bool
                Run sensitivity calculation (requires additional Monte Carlo runs)

            Returns
            -------
            BaseOutputMC object
        '''
        mcout = self.ifunc.calc_MCMC(samples=samples, burnin=kwargs.get('burnin', 0.2))
        samples = mcout.properties.pop('samples')[:, self.pidx]
        samples = np.atleast_2d(samples).T
        if self.mode == 'param':
            self.outputs['mcmc'] = out_uncert.create_output('mcmc', mean=mcout.mean[self.pidx], uncert=mcout.uncert[self.pidx], samples=samples, **mcout.properties)
        elif self.mode == 'pred':
            self.outputs['mcmc'] = out_uncert.create_output('mcmc', mean=mcout.y(self.pidx), uncert=mcout.u_pred(self.pidx), samples=samples, **mcout.properties)
        return self.outputs['mcmc']

    def calc_LSQ(self):
        ''' Calculate analytical Least Squares solution

            Returns
            -------
            BaseOutput object
        '''
        lsqout = self.ifunc.calc_LSQ()
        if self.mode == 'param':
            self.outputs['lsq'] = out_uncert.create_output('lsq', mean=lsqout.mean[self.pidx], uncert=lsqout.uncert[self.pidx], **lsqout.properties)
        elif self.mode == 'pred':
            self.outputs['lsq'] = out_uncert.create_output('lsq', mean=lsqout.y(self.pidx), uncert=lsqout.u_pred(self.pidx), **lsqout.properties)
        return self.outputs['lsq']


def _get_uconf(x, coeff, cov, func):
    ''' Calculate confidence band for fit curve for arbitrary nonlinear regression.

        Parameters
        ----------
        x: float or array
            x-value at which to determine confidence band
        coeff: array
            Best fit coefficient values for fit function
        cov: array
            Covariance matrix of fit parameters [sigmas should be sqrt(diag(cov)) here]
        func: callable
            Fit function

        Returns
        -------
        uconf: array
            Confidence band at the points in x array. Interval will be
            y +/- k * uconf.

        Reference
        ---------
        Christopher Cox and Guangqin Ma. Asymptotic Confidence Bands for Generalized
        Nonlinear Regression Models. Biometrics Vol. 51, No. 1 (March 1995) pp 142-150.
    '''
    sigmas = np.sqrt(np.diag(cov))
    dp = sigmas / 1E6
    conf = []
    for xval in np.atleast_1d(x):
        grad = scipy.optimize.approx_fprime(coeff, lambda p: func(xval, *p), epsilon=dp)
        conf.append(grad.T @ cov @ grad)
    conf = np.sqrt(np.array(conf))
    return conf[0] if np.isscalar(x) else conf


def _get_upred(x, coeff, cov, func, Syx, sigy, xdata=None, mode='Syx'):
    ''' Calculate prediction band for fit curve for arbitrary nonlinear regression.

        Parameters
        ----------
        x: float or array
            x-value at which to determine confidence band
        coeff: array
            Best fit coefficient values for fit function
        cov: array
            Covariance matrix of fit parameters [sigmas should be sqrt(diag(cov)) here]
        func: callable
            Fit function
        Syx: float
            Uncertainty in y calculated using residuals. Used when mode == 'Syx'
        sigy: array
            Uncertainty in each measured y value. Used when mode == 'sigy' or 'sigylast'.
            Must be paired with xdata parameter.
        xdata: array
            Original measured x values. Used to interpolate Syx when it is non-constant array.
        mode: string
            How to apply uncertainty in new measurement. 'Syx' will use Syx calculated from
            residuals. 'sigy' uses user-provided y-uncertainty, extrapolating between
            values as necessary. 'sigylast' uses last sigy value (useful when x is time
            and fit is being predicted into the future)

        Returns
        -------
        upred: array
            Prediction band at the points in x array. Interval will be
            y +/- k * uconf.

        Reference
        ---------
        Christopher Cox and Guangqin Ma. Asymptotic Confidence Bands for Generalized
        Nonlinear Regression Models. Biometrics Vol. 51, No. 1 (March 1995) pp 142-150.
    '''
    if mode not in ['Syx', 'sigy', 'sigylast']:
        raise ValueError('Prediction band mode must be Syx, sigy, or sigylast')

    if mode == 'Syx' or (np.isscalar(sigy) and sigy==0):
        uy = Syx
    elif np.isscalar(sigy):
        uy = sigy
    elif mode == 'sigy':
        if sigy.min() == sigy.max():  # All elements equal
            uy = sigy[0]
        else:
            if not np.all(np.diff(xdata) > 0):  # np.interp requires sorted data
                idx = np.argsort(xdata)
                xdata = xdata[idx]
                sigy = sigy[idx]
            uy = interpolate.interp1d(xdata, sigy, fill_value='extrapolate')(x)
    elif mode == 'sigylast':
        uy = sigy[-1]
    return np.sqrt(_get_uconf(x, coeff, cov, func)**2 + uy**2)


def _get_upred_MC(x, Syx, sigy, u_conf, xdata=None, mode='Syx'):
    ''' Calculate prediction band for fit curve based on sampled Monte Carlo data.

        Parameters
        ----------
        x: float or array
            x-value at which to determine confidence band
        u_conf: callable
            Function for determining u_conf for this x value
        Syx: float
            Uncertainty in y calculated using residuals. Used when mode == 'Syx'
        sigy: array
            Uncertainty in each measured y value. Used when mode == 'sigy' or 'sigylast'.
            Must be paired with xdata parameter.
        xdata: array
            Original measured x values. Used to interpolate Syx when it is non-constant array.
        mode: string
            How to apply uncertainty in new measurement. 'Syx' will use Syx calculated from
            residuals. 'sigy' uses user-provided y-uncertainty, extrapolating between
            values as necessary. 'sigylast' uses last sigy value (useful when x is time
            and fit is being predicted into the future)

        Returns
        -------
        uconf: array
            Confidence band at the points in x array. Interval will be
            y +/- k * uconf.
    '''
    if mode not in ['Syx', 'sigy', 'sigylast']:
        raise ValueError('Prediction band mode must be Syx, sigy, or sigylast')

    if mode == 'Syx' or (np.isscalar(sigy) and sigy == 0):
        uy = Syx
    elif np.isscalar(sigy):
        uy = sigy
    elif mode == 'sigy':
        idx = np.argsort(xdata)
        xdata = xdata[idx]
        sigysort = sigy[idx]
        uy = interpolate.interp1d(xdata, sigysort, fill_value='extrapolate')(x)
    elif mode == 'sigylast':
        uy = sigy[-1]
    return np.sqrt(u_conf(x)**2 + uy**2)


# Functions for fitting curves
#------------------------------------------------------------
def odrfit(func, x, y, ux, uy, p0=None, absolute_sigma=True):
    ''' Fit the curve using scipy's orthogonal distance regression (ODR)

        Parameters
        ----------
        func: callable
            The function to fit. Must take x as first argument, and other
            parameters as remaining arguments.
        x, y: arrays
            X and Y data to fit
        ux, uy: arrays
            Standard uncertainty in x and y
        p0: array
            Initial guess of parameters.
        absolute_sigma: boolean
            Treat uncertainties in an absolute sense. If false, only relative
            magnitudes matter.

        Returns
        -------
        pcoeff: array
            Coefficients of best fit curve
        pcov: array
            Covariance of coefficients. Standard error of coefficients is
            np.sqrt(np.diag(pcov)).
    '''
    # Wrap the function because ODR puts params first, x last
    def odrfunc(B, x):
        return func(x, *B)

    if ux is not None and all(ux == 0):
        ux = None
    if uy is not None and all(uy == 0):
        uy = None

    model = odr.Model(odrfunc)
    mdata = odr.RealData(x, y, sx=ux, sy=uy)
    modr = odr.ODR(mdata, model, beta0=p0)
    mout = modr.run()
    if mout.info != 1:
        print('Warning - ODR failed to converge')

    if absolute_sigma:
        # SEE: https://github.com/scipy/scipy/issues/6842.
        # If this issue is fixed, these options may be swapped!
        cov = mout.cov_beta
    else:
        cov = mout.cov_beta*mout.res_var
    return mout.beta, cov


def genfit(func, x, y, ux, uy, p0=None, method=None, bounds=(-np.inf, np.inf), odr=None, absolute_sigma=True):
    ''' Generic curve fit. Selects scipy.optimize.curve_fit if ux==0 or scipy.odr otherwise.

        Parameters
        ----------
        func: callable
            The function to fit
        x, y: arrays
            X and Y data to fit
        ux, uy: arrays
            Standard uncertainty in x and y
        p0: array-like
            Initial guess parameters
        absolute_sigma: boolean
            Treat uncertainties in an absolute sense. If false, only relative
            magnitudes matter.

        Returns
        -------
        pcoeff: array
            Coefficients of best fit curve
        pcov: array
            Covariance of coefficients. Standard error of coefficients is
            np.sqrt(np.diag(pcov)).
    '''
    if odr or not (ux is None or all(ux == 0)):
        return odrfit(func, x, y, ux, uy, p0=p0, absolute_sigma=absolute_sigma)
    else:
        if uy is None or all(uy == 0):
            return scipy.optimize.curve_fit(func, x, y, p0=p0, bounds=bounds)
        else:
            return scipy.optimize.curve_fit(func, x, y, sigma=uy, absolute_sigma=absolute_sigma, p0=p0, bounds=bounds)


def genlinefit(x, y, ux, uy, absolute_sigma=True):
    ''' Generic straight line fit. Uses linefit() if ux==0 or linefitYork otherwise.

        Parameters
        ----------
        func: callable
            The function to fit
        x, y: arrays
            X and Y data to fit
        ux, uy: arrays
            Standard uncertainty in x and y
        absolute_sigma: boolean
            Treat uncertainties in an absolute sense. If false, only relative
            magnitudes matter.

        Returns
        -------
        pcoeff: array
            Coefficients of best fit curve
        pcov: array
            Covariance of coefficients. Standard error of coefficients is
            np.sqrt(np.diag(pcov)).
    '''
    if ux is None or all(ux == 0):
        return linefit(x, y, sig=uy, absolute_sigma=absolute_sigma)
    else:
        return linefitYork(x, y, sigx=ux, sigy=uy, absolute_sigma=absolute_sigma)


def linefit(x, y, sig, absolute_sigma=True):
    ''' Fit a line with uncertainty in y (but not x)

        Parameters
        ----------
        x: array
            X values of fit
        y: array
            Y values of fit
        sig: array
            uncertainty in y values
        absolute_sigma: boolean
            Treat uncertainties in an absolute sense. If false, only relative
            magnitudes matter.

        Returns
        -------
        coeff: array
            Coefficients of line fit [slope, intercept].
        cov: array 2x2
            Covariance matrix of fit parameters. Standard error is
            np.sqrt(np.diag(cov)).

        Note
        ----
        Returning coeffs and covariance so the return value matches scipy.optimize.curve_fit.
        With sig=0, this algorithm estimates a sigma using the residuals.

        References
        ----------
        [1] Numerical Recipes in C, The Art of Scientific Computing. Second Edition.
            W. H. Press, S. A. Teukolsky, W. T. Vetterling, B. P. Flannery.
            Cambridge University Press. 2002.
    '''
    sig = np.atleast_1d(sig)
    if len(sig) == 1:
        sig = np.full(len(x), sig[0])
    if all(sig) > 0:
        wt = 1./sig**2
        ss = sum(wt)
        sx = sum(x*wt)
        sy = sum(y*wt)
        sxoss = sx/ss
        t = (x-sxoss)/sig
        st2 = sum(t*t)
        b = sum(t*y/sig)/st2
    else:
        sx = sum(x)
        sy = sum(y)
        ss = len(x)
        sxoss = sx/ss
        t = (x-sxoss)
        st2 = sum(t*t)
        b = sum(t*y)/st2
    a = (sy-sx*b)/ss
    siga = np.sqrt((1+sx*sx/(ss*st2))/ss)
    sigb = np.sqrt(1/st2)

    resid = sum((y-a-b*x)**2)
    syx = np.sqrt(resid/(len(x)-2))
    cov = -sxoss * sigb**2
    if not all(sig) > 0:
        siga = siga * syx
        sigb = sigb * syx
        cov = cov * syx*syx
    elif not absolute_sigma:
        # See note in scipy.optimize.curve_fit for absolute_sigma parameter.
        chi2 = sum(((y-a-b*x)/sig)**2)/(len(x)-2)
        siga, sigb, cov = np.sqrt(siga**2*chi2), np.sqrt(sigb**2*chi2), cov*chi2
    #rab = -sxoss * sigb / siga  # Correlation can be computed this way
    return np.array([b, a]), np.array([[sigb**2, cov], [cov, siga**2]])


def linefitYork(x, y, sigx=None, sigy=None, rxy=None, absolute_sigma=True):
    ''' Find a best-fit line through the x, y points having
        uncertainties in both x and y. Also accounts for
        correlation between the uncertainties. Uses York's algorithm.

        Parameters
        ----------
        x: array
            X values to fit
        y: array
            Y values to fit
        sigx: array or float
            Uncertainty in x values
        sigy: array or float
            Uncertainty in y values
        rxy: array or float, optional
            Correlation coefficient between sigx and sigy
        absolute_sigma: boolean
            Treat uncertainties in an absolute sense. If false, only relative
            magnitudes matter.

        Returns
        -------
        coeff: array
            Coefficients of line fit [slope, intercept].
        cov: array 2x2
            Covariance matrix of fit parameters. Standard error is
            np.sqrt(np.diag(cov)).

        Note
        ----
        Returning coeffs and covariance so the return value matches scipy.optimize.curve_fit.
        Implemented based on algorithm in [1] and pseudocode in [2].

        References
        ----------
        [1] York, Evensen. Unified equations for the slope, intercept, and standard
            errors of the best straight line. American Journal of Physics. 72, 367 (2004)
        [2] Wehr, Saleska. The long-solved problem of the best-fit straight line:
            application to isotopic mixing lines. Biogeosciences. 14, 17-29 (2017)
    '''
    # Condition inputs so they're all float64 arrays
    x = x.astype(np.float64)
    y = y.astype(np.float64)
    if sigx is None or len(np.nonzero(sigx)[0]) == 0:
        sigx = np.full(len(x), 1E-99, dtype=np.float64)   # Don't use 0, but a really small number
    elif np.isscalar(sigx):
        sigx = np.full_like(x, sigx)

    if sigy is None or len(np.nonzero(sigy)[0]) == 0:
        sigy = np.full(len(y), 1E-99, dtype=np.float64)
    elif np.isscalar(sigy):
        sigy = np.full_like(x, sigy)

    sigy = np.maximum(sigy, 1E-99)
    sigx = np.maximum(sigx, 1E-99)

    if rxy is None:
        rxy = np.zeros_like(y)
    elif np.isscalar(rxy):
        rxy = np.full_like(x, rxy)

    _, b0 = np.polyfit(x, y, deg=1)  # Get initial estimate for slope
    T = 1E-15
    b = b0
    bdiff = np.inf

    wx = 1./sigx**2
    wy = 1./sigy**2
    alpha = np.sqrt(wx*wy)
    while bdiff > T:
        bold = b
        w = alpha**2/(b**2 * wy + wx - 2*b*rxy*alpha)
        sumw = sum(w)
        X = sum(w*x)/sumw
        Y = sum(w*y)/sumw
        U = x - X
        V = y - Y
        beta = w * (U/wy + b*V/wx - (b*U + V)*rxy/alpha)
        Q1 = sum(w*beta*V)
        Q2 = sum(w*beta*U)
        b = Q1/Q2
        bdiff = abs((b-bold)/bold)
    a = Y - b*X

    # Uncertainties
    xi = X + beta
    xbar = sum(w*xi) / sumw
    sigb = np.sqrt(1./sum(w * (xi - xbar)**2))
    siga = np.sqrt(xbar**2 * sigb**2 + 1/sumw)
    #resid = sum((y-b*x-a)**2)

    # Correlation bw a, b
    #rab = -xbar * sigb / siga
    cov = -xbar * sigb**2

    if not absolute_sigma:
        # See note in scipy.optimize.curve_fit for absolute_sigma parameter.
        chi2 = sum(((y-a-b*x)*np.sqrt(w))**2)/(len(x)-2)
        siga, sigb, cov = np.sqrt(siga**2*chi2), np.sqrt(sigb**2*chi2), cov*chi2
    return np.array([b, a]), np.array([[sigb**2, cov], [cov, siga**2]])