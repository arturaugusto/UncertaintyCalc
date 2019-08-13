''' Calculations for risk, including probability of false accept (PFA) or consumer risk,
    and probability of false reject (PFR) or producer risk.

    The PFA and PFR functions take arbitrary distributions and perform the false accept/
    false reject double integrals numerically. Distributions can be either frozen instances
    of scipy.stats or random samples (e.g. Monte Carlo output of a forward uncertainty
    propagation calculation). PFAR_MC will find both PFA and PFR using a Monte Carlo method.

    The functions PFA_norm and PFR_norm assume normal distributions and take
    TUR and in-tolerance-probability (itp) as inputs. The normal assumption will
    make these functions much faster than the generic PFA and PFR functions.
    The functions PFA_deaver and PFR_deaver use the equations in Deaver's "How to
    "Maintain Confidence" paper, which require specification limits in terms of
    standard deviations of the process distribution, and use a slightly different
    definition for TUR. These functions are provided for convienience when working
    with this definition.

    The guardband and guardband_norm functions can be used to determine the guardband
    required to meet a specified PFA, or apply one of the common guardband calculation
    techniques.

    The Risk and RiskOutput classes are included mainly for use with the GUI and
    project interface, and provide consistent wrappers around the other risk
    calculation functions.
'''

import numpy as np
import yaml
from scipy import stats
import matplotlib as mpl
from matplotlib.ticker import FormatStrFormatter
import matplotlib.pyplot as plt
from scipy.integrate import dblquad
from scipy.optimize import brentq, fsolve

from . import output
from . import customdists


def process_risk(dist, LL, UL):
    ''' Calculate process risk and process capability index for the distribution.

        Parameters
        ----------
        dist: stats.rv_frozen
            Distribution of possible unit under test values
        LL: float
            Lower specification limit
        UL: float
            Upper specification limit

        Returns
        -------
        Cpk: float
            Process capability index. Cpk > 1.333 indicates process is capable of
            meeting specifications.
        risk_total: float
            Total risk (0-1 range) of nonconformance
        risk_lower: float
            Risk of nonconformance below LL
        risk_upper: float
            Risk of nonconformance above UL

        Notes
        -----
        Normal distributions use the standard definition for cpk:

            min( (UL - x)/(3 sigma), (x - LL)/(3 sigma) )

        Non-normal distributions use the proportion nonconforming:

            min( norm.ppf(risk_lower)/3, norm.ppf(risk_upper)/3 )

        (See https://www.qualitydigest.com/inside/quality-insider-article/process-performance-indices-nonnormal-distributions.html)
    '''
    LL, UL = min(LL, UL), max(LL, UL)  # make sure LL < UL
    risk_lower = dist.cdf(LL)
    risk_upper = 1 - dist.cdf(UL)
    risk_total = risk_lower + risk_upper
    if dist.dist.name == 'norm':
        # Normal distributions can use the standard definition of cpk, process capability index
        cpk = min((UL-dist.mean())/(3*dist.std()), (dist.mean()-LL)/(3*dist.std()))
    else:
        # Non-normal distributions use fractions out.
        # See https://www.qualitydigest.com/inside/quality-insider-article/process-performance-indices-nonnormal-distributions.html
        cpk = max(0, min(abs(stats.norm.ppf(risk_lower))/3, abs(stats.norm.ppf(risk_upper))/3))
        if risk_lower > .5 or risk_upper > .5:
            cpk = -cpk
    return cpk, risk_total, risk_lower, risk_upper


def guardband_norm(method, TUR, **kwargs):
    ''' Get guardband factor for the TUR (applies to normal-only risk).

        Parameters
        ----------
        method: string
            Guard band method to apply. One of: 'dobbert', 'rss',
            'rp10', 'test', '4:1', 'pfa'.
        TUR: float
            Test Uncertainty Ratio

        Keyword Arguments
        -----------------
        pfa: float (optional)
            Target PFA (for method 'pfa'. Defaults to 0.008)
        itp: float (optional)
            In-tolerance probability (for method 'pfa' and '4:1'. Defaults to 0.95)

        Returns
        -------
        guardband: (float)
            Guard band factor. Acceptance limit = Tolerance Limit * guardband.

        Notes
        -----
        Dobbert's method maintains <2% PFA for ANY itp at the TUR.
        RSS method: GB = sqrt(1-1/TUR**2)
        test method: GB = 1 - 1/TUR  (subtract the 95% test uncertainty)
        rp10 method: GB = 1.25 - 1/TUR (similar to test, but less conservative)
        pfa method: Solve for GB to produce desired PFA
        4:1 method: Solve for GB that results in same PFA as 4:1 at this itp
    '''
    if method == 'dobbert':
        # Dobbert Eq. 4 for Managed Guard Band, maintains max PFA 2% for any itp.
        M = 1.04 - np.exp(0.38 * np.log(TUR) - 0.54)
        GB = 1 - M / TUR
    elif method == 'rss':
        # The common RSS method
        GB = np.sqrt(1-1/TUR**2)
    elif method == 'test':
        # Subtract the test uncertainty from the spec limit
        GB = 1 - 1/TUR if TUR <= 4 else 1
    elif method == 'rp10':
        # Method described in NCSLI RP-10
        GB = 1.25 - 1/TUR if TUR <= 4 else 1
    elif method in ['pfa', '4:1']:
        # Calculate guardband for specific PFA
        itp = kwargs.get('itp', 0.95)
        if method == 'pfa':
            pfa_target = kwargs.get('pfa', .008)
        else:
            pfa_target = PFA_norm(itp, TUR=4)
        # In normal case, this is faster than guardband() method
        GB = fsolve(lambda x: PFA_norm(itp, TUR, GB=x)-pfa_target, x0=.8)[0]
    else:
        raise ValueError('Unknown guard band method {}.'.format(method))
    return GB


def guardband(dist_proc, dist_test, LL, UL, target_PFA, approx=False):
    ''' Calculate (symmetric) guard band required to meet a target PFA value, for
        arbitrary distributons.

        Parameters
        ----------
        dist_proc: stats.rv_frozen
            Distribution of possible unit under test values from process
        dist_test: stats.rv_frozen
            Distribution of possible test measurement values
        LL: float
            Lower specification limit (absolute)
        UL: float
            Upper specification limit (absolute)
        target_PFA: float
            Probability of false accept required
        approx: bool
            Approximate the integral using discrete probability distribution.
            Faster than using scipy.integrate.

        Returns
        -------
        GB: float
            Guardband offset required to meet target PFA. Symmetric on upper and
            lower limits, such that lower test limit is LL+GB and upper
            test limit is UL-GB.

        Notes
        -----
        Uses Brent's Method to find zero of PFA(dist_proc, dist_test, LL, UL, GBU=x, GBL=x)-target_PFA.
    '''
    # NOTE: This can be slow (several minutes) especially for non-normals. Any way to speed up?
    w = UL-(LL+UL)/2
    try:
        gb, r = brentq(lambda x: PFA(dist_proc, dist_test, LL, UL, GBU=x, GBL=x, approx=approx)-target_PFA, a=-w/2, b=w/2, full_output=True)
    except ValueError:
        return np.nan  # Problem solving

    if r.converged:
        return gb
    else:
        return np.nan


def PFA_norm(itp, TUR, GB=1, **kwargs):
    ''' PFA for normal distributions in terms of TUR and
        in-tolerance probability

        Parameters
        ----------
        itp: float
            In-tolerance probability (0-1 range). A-priori distribution of
            process.
        TUR: float
            Test Uncertainty Ratio. Spec Limit / (2*Test Uncertainty)
        GB: float or string (optional)
            Guard Band Factor. If GB is numeric, GB = K, where acceptance
            limit A = T * K. In Dobbert's notation, K = 1 - M/TUR where
            A = T - U*M. GB = 1 implies no guardbanding.

            If GB is a string, it can be one of options in get_guardband
            method. kwargs passed to get_guardband.
    '''
    # Convert itp to stdev of process
    # This is T in equation 2 in Dobbert's Guard Banding Strategy, with T = 1.
    sigma0 = 1/stats.norm.ppf((1+itp)/2)
    sigmatest = 1/TUR/2

    try:
        GB = float(GB)
    except ValueError:  # String
        GB = guardband_norm(GB, TUR, itp=itp, **kwargs)

    A = GB  # A = T * GB = 1 * GB
    c, _ = dblquad(lambda y, t: np.exp((-y*y)/2/sigma0**2)*np.exp(-(t-y)**2/2/sigmatest**2),
                   -A, A, gfun=lambda t: 1, hfun=lambda t: np.inf)
    c = c / (2 * np.pi * sigmatest * sigma0)
    return c * 2


def PFR_norm(itp, TUR, GB=1, **kwargs):
    ''' PFR for normal distributions in terms of TUR and
        in-tolerance probability

        Parameters
        ----------
        itp: float
            In-tolerance probability (0-1 range). A-priori distribution of
            process.
        TUR: float
            Test Uncertainty Ratio. Spec Limit / (2*Test Uncertainty)
        GB: float or string (optional)
            Guard Band Factor. If GB is numeric, GB = K, where acceptance
            limit A = T * K. In Dobbert's notation, K = 1 - M/TUR where
            A = T - U*M. GB = 1 implies no guardbanding.

            If GB is a string, it can be one of options in get_guardband
            method. kwargs passed to get_guardband.
    '''
    sigma0 = 1/stats.norm.ppf((1+itp)/2)
    sigmatest = 1/TUR/2

    try:
        GB = float(GB)
    except ValueError:  # String
        GB = guardband_norm(GB, TUR, itp=itp, **kwargs)

    A = GB
    c, _ = dblquad(lambda y, t: np.exp((-y*y)/2/sigma0**2)*np.exp(-(t-y)**2/2/sigmatest**2),
                   A, np.inf, gfun=lambda t: -1, hfun=lambda t: 1)
    c = c / (2 * np.pi * sigmatest * sigma0)
    return c * 2


def PFA_deaver(SL, TUR, GB=1):
    ''' Calculate Probability of False Accept (Consumer Risk) for normal
        distributions given spec limit and TUR, using Deaver's equation.

        Parameters
        ----------
        sigma: float
            Specification Limit in terms of standard deviations, symmetric on
            each side of the mean
        TUR: float
            Test Uncertainty Ratio (sigma_uut / sigma_test). Note this is
            definition used by Deaver's papers, NOT the typical SL/(2*sigma_test) definition.
        GB: float (optional)
            Guard Band factor (0-1) with 1 being no guard band

        Returns
        -------
        PFA: float
            Probability of False Accept

        Reference
        ---------
        Equation 6 in Deaver - How to Maintain Confidence
    '''
    c, _ = dblquad(lambda y, t: np.exp(-(y*y + t*t)/2) / np.pi, SL, np.inf, gfun=lambda t: -TUR*(t+SL*GB), hfun=lambda t: -TUR*(t-SL*GB))
    return c


def PFR_deaver(SL, TUR, GB=1):
    ''' Calculate Probability of False Reject (Producer Risk) for normal
        distributions given spec limit and TUR, using Deaver's equation.

        Parameters
        ----------
        SL: float
            Specification Limit in terms of standard deviations, symmetric on
            each side of the mean
        TUR: float
            Test Uncertainty Ratio (sigma_uut / sigma_test). Note this is
            definition used by Deaver's papers, NOT the typical SL/(2*sigma_test) definition.
        GB: float (optional)
            Guard Band factor (0-1) with 1 being no guard band

        Returns
        -------
        PFR: float
            Probability of False Reject

        Reference
        ---------
        Equation 7 in Deaver - How to Maintain Confidence
    '''
    p, _ = dblquad(lambda y, t: np.exp(-(y*y + t*t)/2) / np.pi, -SL, SL, gfun=lambda t: TUR*(GB*SL-t), hfun=lambda t: np.inf)
    return p


def PFA(dist_proc, dist_test, LL, UL, GBL=0, GBU=0, approx=False):
    ''' Calculate Probability of False Accept (Consumer Risk) for arbitrary
        process and test distributions.

        Parameters
        ----------
        dist_proc: stats.rv_frozen
            Distribution of possible unit under test values from process
        dist_test: stats.rv_frozen
            Distribution of possible test measurement values
        LL: float
            Lower specification limit (absolute)
        UL: float
            Upper specification limit (absolute)
        GBL: float
            Lower guard band, as offset. Test limit is LL + GBL.
        GBU: float
            Upper guard band, as offset. Test limit is UL - GBU.
        approx: bool
            Approximate using discrete probability distribution. This
            uses trapz integration so it may be faster than letting
            scipy integrate the actual pdf function.

        Returns
        -------
        PFA: float
            Probability of False Accept
    '''
    if approx:
        xx = np.linspace(dist_proc.median() - dist_proc.std()*8, dist_proc.median() + dist_proc.std()*8, num=1000)
        xx2 = np.linspace(dist_test.median() - dist_test.std()*8,  dist_test.median() + dist_test.std()*8, num=1000)
        return _PFA_discrete((xx, dist_proc.pdf(xx)), (xx2, dist_test.pdf(xx2)), LL, UL, GBL=GBL, GBU=GBU)

    else:
        # Strip loc keyword from test distribution so it can be changed,
        # but shift loc so the MEDIAN (expected) value starts at the spec limit.
        median = dist_test.median()
        kwds = customdists.get_distargs(dist_test)
        locorig = kwds.pop('loc', 0)

        def integrand(y, t):
            return dist_test.dist.pdf(y, loc=t-(median-locorig), **kwds) * dist_proc.pdf(y)

        c1, _ = dblquad(integrand, LL+GBL, UL-GBU, gfun=lambda t: UL, hfun=lambda t: np.inf)
        c2, _ = dblquad(integrand, LL+GBL, UL-GBU, gfun=lambda t: -np.inf, hfun=lambda t: LL)
        return c1 + c2


def _PFA_discrete(dist_proc, dist_test, LL, UL, GBL=0, GBU=0):
    ''' Calculate Probability of False Accept (Consumer Risk) using
        sampled distributions.

        Parameters
        ----------
        dist_proc: array
            Sampled values from process distribution
        dist_test: array
            Sampled values from test measurement distribution
        LL: float
            Lower specification limit (absolute)
        UL: float
            Upper specification limit (absolute)
        GBL: float
            Lower guard band, as offset. Test limit is LL + GBL.
        GBU: float
            Upper guard band, as offset. Test limit is UL - GBU.

        Returns
        -------
        PFA: float
            Probability of False Accept
    '''
    if isinstance(dist_proc, tuple):
        procx, procy = dist_proc
        dy = procx[1]-procx[0]
    else:
        procy, procx = np.histogram(dist_proc, bins='auto', density=True)
        dy = procx[1]-procx[0]
        procx = procx[1:] - dy/2

    if isinstance(dist_test, tuple):
        testx, testy = dist_test
        dx = testx[1]-testx[0]
    else:
        testy, testx = np.histogram(dist_test, bins='auto', density=True)
        dx = testx[1]-testx[0]
        testx = testx[1:] - dx/2

    testmed = np.median(testx)
    c = 0
    for t, ut in zip(procx[np.where(procx > UL)], procy[np.where(procx > UL)]):
        idx = np.where(testx+t-testmed < UL-GBU)
        c += np.trapz(ut*testy[idx], dx=dx)

    for t, ut in zip(procx[np.where(procx < LL)], procy[np.where(procx < LL)]):
        idx = np.where(testx+t-testmed > LL+GBL)
        c += np.trapz(ut*testy[idx], dx=dx)

    c *= dy
    return c


def PFR(dist_proc, dist_test, LL, UL, GBL=0, GBU=0, approx=False):
    ''' Calculate Probability of False Reject (Producer Risk) for arbitrary
        process and test distributions.

        Parameters
        ----------
        dist_proc: stats.rv_frozen
            Distribution of possible unit under test values from process
        dist_test: stats.rv_frozen
            Distribution of possible test measurement values
        LL: float
            Lower specification limit (absolute)
        UL: float
            Upper specification limit (absolute)
        GBL: float
            Lower guard band, as offset. Test limit is LL + GBL.
        GBU: float
            Upper guard band, as offset. Test limit is UL - GBU.
        approx: bool
            Approximate using discrete probability distribution. This
            uses trapz integration so it may be faster than letting
            scipy integrate the actual pdf function.

        Returns
        -------
        PFR: float
            Probability of False Reject
    '''
    if approx:
        xx = np.linspace(dist_proc.median() - dist_proc.std()*8, dist_proc.median() + dist_proc.std()*8, num=1000)
        xx2 = np.linspace(dist_test.median() - dist_test.std()*8,  dist_test.median() + dist_test.std()*8, num=1000)
        return _PFR_discrete((xx, dist_proc.pdf(xx)), (xx2, dist_test.pdf(xx2)), LL, UL, GBL=GBL, GBU=GBU)

    else:
        # Strip loc keyword from test distribution so it can be changed,
        # but shift loc so the MEDIAN value starts at the spec limit.
        median = dist_test.median()
        kwds = customdists.get_distargs(dist_test)
        locorig = kwds.pop('loc', 0)

        def integrand(y, t):
            return dist_test.dist.pdf(y, loc=t-(median-locorig), **kwds) * dist_proc.pdf(y)

        p1, _ = dblquad(integrand, UL-GBU, np.inf, gfun=lambda t: LL, hfun=lambda t: UL)
        p2, _ = dblquad(integrand, -np.inf, LL+GBL, gfun=lambda t: LL, hfun=lambda t: UL)
        return p1 + p2


def _PFR_discrete(dist_proc, dist_test, LL, UL, GBL=0, GBU=0):
    ''' Calculate Probability of False Reject (Producer Risk) using
        sampled distributions.

        Parameters
        ----------
        dist_proc: array
            Sampled values from process distribution
        dist_test: array
            Sampled values from test measurement distribution
        LL: float
            Lower specification limit (absolute)
        UL: float
            Upper specification limit (absolute)
        GBL: float
            Lower guard band, as offset. Test limit is LL + GBL.
        GBU: float
            Upper guard band, as offset. Test limit is UL - GBU.

        Returns
        -------
        PFR: float
            Probability of False Reject
    '''
    if isinstance(dist_proc, tuple):
        procx, procy = dist_proc
        dy = procx[1]-procx[0]
    else:
        procy, procx = np.histogram(dist_proc, bins='auto', density=True)
        dy = procx[1]-procx[0]
        procx = procx[1:] - dy/2

    if isinstance(dist_test, tuple):
        testx, testy = dist_test
        dx = testx[1]-testx[0]
    else:
        testy, testx = np.histogram(dist_test, bins='auto', density=True)
        dx = testx[1]-testx[0]
        testx = testx[1:] - dx/2

    testmed = np.median(testx)
    c = 0
    for t, ut in zip(procx[np.where((procx > LL) & (procx < UL))], procy[np.where((procx > LL) & (procx < UL))]):
        idx = np.where(testx+t-testmed > UL-GBU)
        c += np.trapz(ut*testy[idx], dx=dx)
        idx = np.where(testx+t-testmed < LL+GBL)
        c += np.trapz(ut*testy[idx], dx=dx)

    c *= dy
    return c


def PFAR_MC(dist_proc, dist_test, LL, UL, GBL=0, GBU=0, N=100000, retsamples=False):
    ''' Probability of False Accept/Reject using Monte Carlo Method

        dist_proc: array or stats.rv_frozen
            Distribution of possible unit under test values from process
        dist_test: array or stats.rv_frozen
            Distribution of possible test measurement values
        LL: float
            Lower specification limit (absolute)
        UL: float
            Upper specification limit (absolute)
        GBL: float
            Lower guard band, as offset. Test limit is LL + GBL.
        GBU: float
            Upper guard band, as offset. Test limit is UL - GBU.
        N: int
            Number of Monte Carlo samples
        retsamples: bool
            Return samples along with probabilities

        Returns
        -------
        PFA: float
            False accept probability
        PFR: float
            False reject probability
        proc_samples: array (optional)
            Monte Carlo samples for uut (if retsamples==True)
        test_samples: array (optional)
            Monte Carlo samples for test measurement (if retsamples==True)
    '''
    proc_samples = dist_proc.rvs(size=N)
    median = dist_test.median()
    kwds = customdists.get_distargs(dist_test)
    locorig = kwds.pop('loc', 0)
    test_samples = dist_test.dist.rvs(loc=proc_samples-(median-locorig), **kwds)

    FA = np.count_nonzero(((proc_samples > UL) & (test_samples < UL-GBU)) | ((proc_samples < LL) & (test_samples > LL+GBL)))/N
    FR = np.count_nonzero(((proc_samples < UL) & (test_samples > UL-GBU)) | ((proc_samples > LL) & (test_samples < LL+GBL)))/N

    if retsamples:
        return FA, FR, proc_samples, test_samples
    else:
        return FA, FR


class Risk(object):
    ''' Class incorporating risk calculations. Monstly useful for implementing the GUI.
        Risk Functions in this module can be used more easiliy for manual work.
    '''
    def __init__(self, name='risk'):
        self.name = name
        self.description = ''
        self.procdist = customdists.normal(.51021346)  # For 95% itp starting value
        self.testdist = customdists.normal(0.125)
        self.speclimits = (-1.0, 1.0)  # Upper/lower specification limits
        self.guardband = (0, 0)        # Guard band offset (A = speclimits - guardband)
        self.out = RiskOutput(self)

    def set_testdist(self, testdist):
        ''' Set the test distribution

            Parameters
            ----------
            testdist: stats.rv_continuous
                Test distribution instance
        '''
        self.testdist = testdist

    def set_procdist(self, procdist):
        ''' Set the process distribution

            Parameters
            ----------
            procdist: stats.rv_continuous
                Process distribution instance
        '''
        self.procdist = procdist

    def set_speclimits(self, LL, UL):
        ''' Set specification limits

            Parameters
            ----------
            LL: float
                Lower specification limit, in absolute units
            UL: float
                Upper specification limit, in absolute units
        '''
        self.speclimits = LL, UL

    def set_gbf(self, gbf):
        ''' Set guard band factor

            Parameters
            ----------
            gbf: float
                Guard band factor as multiplier of specification
                limit. Acceptance limit A = T * gbf.
        '''
        rng = (self.speclimits[1] - self.speclimits[0])/2
        gb = rng * (1 - gbf)
        self.guardband = gb, gb

    def set_guardband(self, GBL, GBU):
        ''' Set relative guardband

            Parameters
            ----------
            GBL: float
                Lower guardband offset. Acceptance limit A = LL + GBL
            GBU: float
                Upper guardband offset. Acceptance limit A = UL - GBU
        '''
        self.guardband = GBL, GBU

    def set_itp(self, itp):
        ''' Set in-tolerance probability by adjusting process distribution
            with specification limits of +/-
            Parameters
            ----------
            itp: float
                In-tolerance probability (0-1)
        '''
        self.to_simple()
        sigma = self.speclimits[1] / stats.norm.ppf((1+itp)/2)
        self.procdist = stats.norm(loc=0, scale=sigma)

    def set_tur(self, tur):
        ''' Set test uncertainty ratio by adjusting test distribution

            Parameters
            ----------
            tur: float
                Test uncertainty ratio (> 0)
        '''
        self.to_simple()
        sigma = 1/tur/2
        median = self.testdist.median()
        self.testdist = stats.norm(loc=median, scale=sigma)

    def set_testmedian(self, median):
        ''' Set median of test measurement

            Parameters
            ----------
            median: float
                Median value of a particular test measurement result
        '''
        sigma = self.testdist.std()
        self.testdist = stats.norm(loc=median, scale=sigma)

    def get_testmedian(self):
        ''' Get test measurement median '''
        return self.testdist.median()

    def is_simple(self):
        ''' Check if simplified normal-only functions can be used '''
        if self.procdist is None or self.testdist is None:
            return False
        if self.procdist.median() != 0:
            return False
        if self.procdist.dist.name != 'norm' or self.testdist.dist.name != 'norm':
            return False
        if self.speclimits[1] != 1 or self.speclimits[0] != -1:
            return False
        if self.guardband[1] != self.guardband[0]:
            return False
        return True

    def to_simple(self):
        ''' Convert to simple, normal-only form. '''
        if self.is_simple():
            return  # Already in simple form

        # Get existing parameters
        tur = self.get_tur() if self.testdist is not None else 4
        itp = self.get_itp() if self.procdist is not None else 0.95
        median = self.testdist.median() if self.testdist is not None else 0
        gbf = self.get_gbf()

        # Convert to normal/symmetric
        self.set_speclimits(-1, 1)
        sigma0 = self.speclimits[1] / stats.norm.ppf((1+itp)/2)
        self.procdist = stats.norm(loc=0, scale=sigma0)
        sigmat = 1/tur/2
        self.testdist = stats.norm(loc=median, scale=sigmat)
        self.set_gbf(gbf)

    def get_tur(self):
        ''' Get test uncertainty ratio.
            Speclimit range / Expanded test measurement uncertainty.
        '''
        rng = (self.speclimits[1] - self.speclimits[0])/2   # Half the interval
        TL = self.testdist.std() * 2   # k=2
        return rng/TL

    def get_itp(self):
        ''' Get in-tolerance probability '''
        return 1 - self.proc_risk()

    def get_guardband(self):
        ''' Get guardband as offset GBF where A = T - GBF '''
        return self.guardband

    def get_speclimits(self):
        ''' Get specification limits as absolute values '''
        return self.speclimits

    def get_gbf(self):
        ''' Get guardband as multiplier GB where A = T * GB '''
        gb = self.guardband[1] - (self.guardband[1] - self.guardband[0])/2
        rng = (self.speclimits[1] - self.speclimits[0])/2
        gbf = 1 - gb / rng
        return gbf

    def get_testdist(self):
        ''' Get the test distribution '''
        return self.testdist

    def get_procdist(self):
        ''' Get the process distribution '''
        return self.procdist

    def calc_guardband(self, method, pfa=None):
        ''' Set guardband using a predefined method

        Parameters
        ----------
        method: string
            Guard band method to apply. One of: 'dobbert', 'rss',
            'rp10', 'test', '4:1', 'pfa'.
        TUR: float
            Test Uncertainty Ratio
        pfa: float (optional)
            Target PFA (for method 'pfa'. Defaults to 0.008)

        Notes
        -----
        Dobbert's method maintains <2% PFA for ANY itp at the TUR.
        RSS method: GB = sqrt(1-1/TUR**2)
        test method: GB = 1 - 1/TUR  (subtract the 95% test uncertainty)
        rp10 method: GB = 1.25 - 1/TUR (similar to test, but less conservative)
        pfa method: Solve for GB to produce desired PFA
        4:1 method: Solve for GB that results in same PFA as 4:1 at this itp
        '''
        if self.is_simple() or method != 'pfa':
            gbf = guardband_norm(method, self.get_tur(), pfa=pfa, itp=self.get_itp())
            self.set_gbf(gbf)
        else:
            gb = guardband(self.get_procdist(), self.get_testdist(), *self.get_speclimits(), pfa, approx=True)
            self.set_guardband(gb, gb)   # Returns guardband as offset

    def PFA(self, approx=True):
        ''' Calculate probability of false acceptance (consumer risk).

            Parameters
            ----------
            approx: bool
                Use trapezoidal integration approximation for speed

            Returns
            -------
            PFA: float
                Probability of false accept (0-1)
        '''
        if self.is_simple():
            return PFA_norm(self.get_itp(), self.get_tur(), self.get_gbf())
        else:
            return PFA(self.procdist, self.testdist, *self.speclimits,
                       *self.guardband, approx)

    def PFR(self, approx=True):
        ''' Calculate probability of false reject (producer risk).

            Parameters
            ----------
            approx: bool
                Use trapezoidal integration approximation for speed

            Returns
            -------
            PFR: float
                Probability of false reject (0-1)
        '''
        if self.is_simple():
            return PFR_norm(self.get_itp(), self.get_tur(), self.get_gbf())
        else:
            return PFR(self.procdist, self.testdist, *self.speclimits,
                       *self.guardband, approx)

    def proc_risk(self):
        ''' Calculate total process risk, risk of process distribution being outside
            specification limits
        '''
        return process_risk(self.procdist, *self.speclimits)[1]

    def cpk(self):
        ''' Get process risk and CPK values

        Returns
        -------
        Cpk: float
            Process capability index. Cpk > 1.333 indicates process is capable of
            meeting specifications.
        risk_total: float
            Total risk (0-1 range) of nonconformance
        risk_lower: float
            Risk of nonconformance below LL
        risk_upper: float
            Risk of nonconformance above UL
        '''
        return process_risk(self.procdist, *self.speclimits)

    def test_risk(self):
        ''' Calculate PFA or PFR of the specific test measurement defined by dist_test
            including its median shift. Does not consider process dist. If median(testdist)
            is within spec limits, PFA is returned. If median(testdist) is outside spec
            limits, PFR is returned.

            Returns
            -------
            PFx: float
                Probability of false accept or reject
            accept: bool
                Accept or reject this measurement
        '''
        med = self.testdist.median()
        LL, UL = self.speclimits
        LL, UL = min(LL, UL), max(LL, UL)  # Make sure LL < UL
        accept = (med >= LL + self.guardband[0] and med <= UL - self.guardband[0])

        if med >= LL + self.guardband[0] and med <= UL - self.guardband[0]:
            PFx = self.testdist.cdf(LL) + (1 - self.testdist.cdf(UL))
        else:
            PFx = abs(self.testdist.cdf(LL) - self.testdist.cdf(UL))
        return PFx, accept

    # Extra functions for GUI
    def get_procdist_args(self):
        ''' Get dictionary of arguments for process distribution '''
        return self.get_config()['distproc']

    def get_testdist_args(self):
        ''' Get dictionary of arguments for test distribution '''
        return self.get_config()['disttest']

    # Stuff to make it compatible with UncertCalc projects
    def calculate(self):
        ''' "Calculate" values, returning RiskOutput object '''
        self.out = RiskOutput(self)
        return self.out

    def get_output(self):
        ''' Get output object (or None if not calculated yet) '''
        return self.out

    def get_config(self):
        ''' Get configuration dictionary '''
        d = {}
        d['mode'] = 'risk'
        d['name'] = self.name
        d['desc'] = self.description

        if self.procdist is not None:
            d['distproc'] = customdists.get_config(self.procdist)

        if self.testdist is not None:
            d['disttest'] = customdists.get_config(self.testdist)

        d['GBL'] = self.guardband[0]
        d['GBU'] = self.guardband[1]
        d['LL'] = self.speclimits[0]
        d['UL'] = self.speclimits[1]
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
        ''' Load Risk object from config dictionary '''
        newrisk = cls(name=config.get('name', 'risk'))
        newrisk.description = config.get('desc', '')
        newrisk.set_speclimits(config.get('LL', 0), config.get('UL', 0))
        newrisk.set_guardband(config.get('GBL', 0), config.get('GBU', 0))

        dproc = config.get('distproc', None)
        if dproc is not None:
            dist_proc = customdists.from_config(dproc)
            newrisk.set_procdist(dist_proc)
        else:
            newrisk.procdist = None

        dtest = config.get('disttest', None)
        if dtest is not None:
            dist_test = customdists.from_config(dtest)
            newrisk.set_testdist(dist_test)
        else:
            newrisk.testdist = None
        return newrisk

    @classmethod
    def from_configfile(cls, fname):
        ''' Read and parse the configuration file. Returns a new Risk
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


class RiskOutput(output.Output):
    ''' Output object for risk calculation. Just a reporting wrapper around
        Risk object for parallelism with other calculator modes.
    '''
    def __init__(self, risk, labelsigma=False):
        self.risk = risk
        self.labelsigma = labelsigma

    def report(self, **kwargs):
        ''' Generate report of risk calculation '''
        hdr = []
        cols = []

        if self.risk.get_procdist() is not None:
            cpk, risk_total, risk_lower, risk_upper = self.risk.cpk()
            hdr.extend(['Process Risk'])   # No way to span columns at this point...
            cols.append(['Process Risk: {:.2f}%'.format(risk_total*100),
                         'Upper limit risk: {:.2f}%'.format(risk_upper*100),
                         'Lower limit risk: {:.2f}%'.format(risk_lower*100),
                         'Process capability index (Cpk): {:.6f}'.format(cpk)])

        if self.risk.get_testdist() is not None:
            val = self.risk.get_testdist().median()
            PFx, accept = self.risk.test_risk()  # Get PFA/PFR of specific measurement

            hdr.extend(['Test Measurement Risk'])
            cols.append([
                'TUR: {:.1f}'.format(self.risk.get_tur()),
                'Measured value: {:.4g}'.format(val),
                'Result: {}'.format('ACCEPT' if accept else 'REJECT'),
                'PF{} of measurement: {:.2f}%'.format('A' if accept else 'R', PFx*100),
                ''])

        if self.risk.get_testdist() is not None and self.risk.get_procdist() is not None:
            hdr.extend(['Combined Risk'])
            cols.append([
                'Total PFA: {:.2f}%'.format(self.risk.PFA()*100),
                'Total PFR: {:.2f}%'.format(self.risk.PFR()*100), '', ''])

        if len(hdr) > 0:
            rows = list(map(list, zip(*cols)))  # Transpose cols->rows
            return output.md_table(rows=rows, hdr=hdr)
        else:
            return output.MDstring()

    def report_all(self, **kwargs):
        ''' Report with table and plots '''
        if kwargs.get('mc', False):
            with mpl.style.context(output.mplcontext):
                plt.ioff()
                fig = plt.figure()
            r = self.report_montecarlo(fig=fig, **kwargs)
            r.add_fig(fig)
        else:
            with mpl.style.context(output.mplcontext):
                plt.ioff()
                fig = plt.figure()
                self.plot_dists(fig)
            r = output.MDstring()
            r.add_fig(fig)
            r += self.report(**kwargs)
        return r

    def plot_dists(self, fig=None):
        ''' Plot risk distributions '''
        with mpl.style.context(output.mplcontext):
            plt.ioff()
            if fig is None:
                fig = plt.figure()
            fig.clf()

            procdist = self.risk.get_procdist()
            testdist = self.risk.get_testdist()

            nrows = (procdist is not None) + (testdist is not None)
            plotnum = 0
            LL, UL = self.risk.get_speclimits()
            GBL, GBU = self.risk.get_guardband()

            # Add some room on either side of distributions
            pad = 0
            if procdist is not None:
                pad = max(pad, procdist.std() * 3)
            if testdist is not None:
                pad = max(pad, testdist.std() * 3)

            x = np.linspace(LL - pad, UL + pad, 300)
            if procdist is not None:
                yproc = procdist.pdf(x)
                ax = fig.add_subplot(nrows, 1, plotnum+1)
                ax.plot(x, yproc, label='Process Distribution', color='C0')
                ax.axvline(LL, ls='--', label='Specification Limits', color='C2')
                ax.axvline(UL, ls='--', color='C2')
                ax.fill_between(x, yproc, where=((x <= LL) | (x >= UL)), alpha=.5, color='C0')
                ax.set_ylabel('Probability Density')
                ax.set_xlabel('Value')
                ax.legend(loc='upper left')
                if self.labelsigma:
                    ax.xaxis.set_major_formatter(FormatStrFormatter(r'%d$\sigma$'))
                plotnum += 1

            if testdist is not None:
                ytest = testdist.pdf(x)
                median = self.risk.get_testmedian()
                ax = fig.add_subplot(nrows, 1, plotnum+1)
                ax.plot(x, ytest, label='Test Distribution', color='C1')
                ax.axvline(median, ls='--', color='C1')
                ax.axvline(LL, ls='--', label='Specification Limits', color='C2')
                ax.axvline(UL, ls='--', color='C2')
                if GBL != 0 or GBU != 0:
                    ax.axvline(LL+GBL, ls='--', label='Guard Band', color='C3')
                    ax.axvline(UL-GBU, ls='--', color='C3')

                if median > UL-GBU or median < LL+GBL:   # Shade PFR
                    ax.fill_between(x, ytest, where=((x >= LL) & (x <= UL)), alpha=.5, color='C1')
                else:  # Shade PFA
                    ax.fill_between(x, ytest, where=((x <= LL) | (x >= UL)), alpha=.5, color='C1')

                ax.set_ylabel('Probability Density')
                ax.set_xlabel('Value')
                ax.legend(loc='upper left')
                if self.labelsigma:
                    ax.xaxis.set_major_formatter(FormatStrFormatter(r'%d$\sigma$'))
            fig.tight_layout()
        return fig

    def report_montecarlo(self, fig=None, **kwargs):
        ''' Run Monte-Carlo risk and return report. If fig is provided, plot it. '''
        N = kwargs.get('samples', 100000)
        SL = self.risk.get_speclimits()
        GB = self.risk.get_guardband()
        pfa, pfr, psamples, tsamples = PFAR_MC(self.risk.get_procdist(), self.risk.get_testdist(),
                                               *SL, *GB, N=N, retsamples=True)

        if fig is not None:
            fig.clf()
            ax = fig.add_subplot(1, 1, 1)
            ifr1 = (psamples > SL[0]) & (tsamples < SL[0]+GB[0])
            ifa1 = (psamples < SL[0]) & (tsamples > SL[0]+GB[0])
            ifr2 = (psamples < SL[1]) & (tsamples > SL[1]-GB[1])
            ifa2 = (psamples > SL[1]) & (tsamples < SL[1]-GB[1])
            good = np.logical_not(ifa1 | ifa2 | ifr1 | ifr2)
            ax.plot(psamples[good], tsamples[good], marker='.', ls='', markersize=1, color='C0')
            ax.plot(psamples[ifa1], tsamples[ifa1], marker='.', ls='', markersize=1, color='C1', label='False Accept')
            ax.plot(psamples[ifr1], tsamples[ifr1], marker='.', ls='', markersize=1, color='C2', label='False Reject')
            ax.plot(psamples[ifa2], tsamples[ifa2], marker='.', ls='', markersize=1, color='C1')
            ax.plot(psamples[ifr2], tsamples[ifr2], marker='.', ls='', markersize=1, color='C2')
            ax.axvline(SL[0], ls='--', lw=1, color='black')
            ax.axvline(SL[1], ls='--', lw=1, color='black')
            ax.axhline(SL[0]+GB[0], lw=1, ls='--', color='gray')
            ax.axhline(SL[1]-GB[1], lw=1, ls='--', color='gray')
            ax.axhline(SL[0], ls='--', lw=1, color='black')
            ax.axhline(SL[1], ls='--', lw=1, color='black')
            ax.legend(loc='upper left', fontsize=10)
            ax.set_xlabel('Process Distribution')
            ax.set_ylabel('Test Distribution')

            slrange = SL[1] - SL[0]
            slmin = SL[0] - slrange
            slmax = SL[1] + slrange
            ax.set_xlim(slmin, slmax)
            ax.set_ylim(slmin, slmax)
            fig.tight_layout()

        s = '- TUR: {:.2f}\n- Total PFA: {:.2f}%\n- Total PFR: {:.2f}%'.format(self.risk.get_tur(), pfa*100, pfr*100)
        return output.MDstring(s)