#!/usr/bin/env python
# -*- coding: utf-8 -*-

'''checkplotgen.py - Waqas Bhatti (wbhatti@astro.princeton.edu) - Feb 2019

This contains functions to generate checkplot pickles from a collection of light
curves (optionally including period-finding results).

'''

#############
## LOGGING ##
#############

import logging
from astrobase import log_sub, log_fmt, log_date_fmt

DEBUG = False
if DEBUG:
    level = logging.DEBUG
else:
    level = logging.INFO
LOGGER = logging.getLogger(__name__)
logging.basicConfig(
    level=level,
    style=log_sub,
    format=log_fmt,
    datefmt=log_date_fmt,
)

LOGDEBUG = LOGGER.debug
LOGINFO = LOGGER.info
LOGWARNING = LOGGER.warning
LOGERROR = LOGGER.error
LOGEXCEPTION = LOGGER.exception


#############
## IMPORTS ##
#############

try:
    import cPickle as pickle
except Exception as e:
    import pickle

import sys
import os
import os.path
import glob
import gzip
import uuid
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor

from tornado.escape import squeeze

# to turn a list of keys into a dict address
# from https://stackoverflow.com/a/14692747
from functools import reduce
from operator import getitem
def dict_get(datadict, keylist):
    return reduce(getitem, keylist, datadict)

import numpy as np



############
## CONFIG ##
############

NCPUS = mp.cpu_count()



###################
## LOCAL IMPORTS ##
###################

from astrobase.lcmath import normalize_magseries, sigclip_magseries

from astrobase.checkplot.pkl_io import _write_checkplot_picklefile
from astrobase.checkplot.pkl_utils import (
    _pkl_magseries_plot,
    _pkl_phased_magseries_plot
)
from astrobase.checkplot.pkl import checkplot_dict

from astrobase.lcproc import get_lcformat
from astrobase.lcproc.periodsearch import PFMETHODS


###################################
## CHECKPLOT NEIGHBOR OPERATIONS ##
###################################

# for the neighbors tab in checkplotserver: show a 5 row per neighbor x 3 col
# panel. Each col will have in order: best phased LC of target, phased LC of
# neighbor with same period and epoch, unphased LC of neighbor

def update_checkplotdict_nbrlcs(
        checkplotdict,
        timecol, magcol, errcol,
        lcformat='hat-sql',
        lcformatdir=None,
        verbose=True,
):

    '''For all neighbors in checkplotdict, make LCs and phased LCs.

    Here, we specify the timecol, magcol, errcol explicitly because we're doing
    this per checkplot, which is for a single timecol-magcol-errcol combination.

    '''

    try:
        formatinfo = get_lcformat(lcformat,
                                  use_lcformat_dir=lcformatdir)
        if formatinfo:
            (dfileglob, readerfunc,
             dtimecols, dmagcols, derrcols,
             magsarefluxes, normfunc) = formatinfo
        else:
            LOGERROR("can't figure out the light curve format")
            return checkplotdict
    except Exception as e:
        LOGEXCEPTION("can't figure out the light curve format")
        return checkplotdict

    if not ('neighbors' in checkplotdict and
            checkplotdict['neighbors'] and
            len(checkplotdict['neighbors']) > 0):

        LOGERROR('no neighbors for %s, not updating...' %
                 (checkplotdict['objectid']))
        return checkplotdict

    # get our object's magkeys to compare to the neighbor
    objmagkeys = {}

    # handle diff generations of checkplots
    if 'available_bands' in checkplotdict['objectinfo']:
        mclist = checkplotdict['objectinfo']['available_bands']
    else:
        mclist = ('bmag','vmag','rmag','imag','jmag','hmag','kmag',
                  'sdssu','sdssg','sdssr','sdssi','sdssz')

    for mc in mclist:
        if (mc in checkplotdict['objectinfo'] and
            checkplotdict['objectinfo'][mc] is not None and
            np.isfinite(checkplotdict['objectinfo'][mc])):

            objmagkeys[mc] = checkplotdict['objectinfo'][mc]


    # if there are actually neighbors, go through them in order
    for nbr in checkplotdict['neighbors']:

        objectid, lcfpath = (nbr['objectid'],
                             nbr['lcfpath'])

        # get the light curve
        if not os.path.exists(lcfpath):
            LOGERROR('objectid: %s, neighbor: %s, '
                     'lightcurve: %s not found, skipping...' %
                     (checkplotdict['objectid'], objectid, lcfpath))
            continue

        lcdict = readerfunc(lcfpath)

        # this should handle lists/tuples being returned by readerfunc
        # we assume that the first element is the actual lcdict
        # FIXME: figure out how to not need this assumption
        if ( (isinstance(lcdict, (list, tuple))) and
             (isinstance(lcdict[0], dict)) ):
            lcdict = lcdict[0]


        # 0. get this neighbor's magcols and get the magdiff and colordiff
        # between it and the object

        nbrmagkeys = {}

        for mc in objmagkeys:

            if (('objectinfo' in lcdict) and
                (isinstance(lcdict['objectinfo'], dict)) and
                (mc in lcdict['objectinfo']) and
                (lcdict['objectinfo'][mc] is not None) and
                (np.isfinite(lcdict['objectinfo'][mc]))):

                nbrmagkeys[mc] = lcdict['objectinfo'][mc]

        # now calculate the magdiffs
        magdiffs = {}
        for omc in objmagkeys:
            if omc in nbrmagkeys:
                magdiffs[omc] = objmagkeys[omc] - nbrmagkeys[omc]

        # calculate colors and colordiffs
        colordiffs = {}

        # generate the list of colors to get
        # NOTE: here, we don't really bother with new/old gen checkplots
        # maybe change this later to handle arbitrary colors

        for ctrio in (['bmag','vmag','bvcolor'],
                      ['vmag','kmag','vkcolor'],
                      ['jmag','kmag','jkcolor'],
                      ['sdssi','jmag','ijcolor'],
                      ['sdssg','kmag','gkcolor'],
                      ['sdssg','sdssr','grcolor']):
            m1, m2, color = ctrio

            if (m1 in objmagkeys and
                m2 in objmagkeys and
                m1 in nbrmagkeys and
                m2 in nbrmagkeys):

                objcolor = objmagkeys[m1] - objmagkeys[m2]
                nbrcolor = nbrmagkeys[m1] - nbrmagkeys[m2]
                colordiffs[color] = objcolor - nbrcolor

        # finally, add all the color and magdiff info to the nbr dict
        nbr.update({'magdiffs':magdiffs,
                    'colordiffs':colordiffs})

        #
        # process magcols
        #

        # normalize using the special function if specified
        if normfunc is not None:
            lcdict = normfunc(lcdict)

        try:

            # get the times, mags, and errs
            # dereference the columns and get them from the lcdict
            if '.' in timecol:
                timecolget = timecol.split('.')
            else:
                timecolget = [timecol]
            times = dict_get(lcdict, timecolget)

            if '.' in magcol:
                magcolget = magcol.split('.')
            else:
                magcolget = [magcol]
            mags = dict_get(lcdict, magcolget)

            if '.' in errcol:
                errcolget = errcol.split('.')
            else:
                errcolget = [errcol]
            errs = dict_get(lcdict, errcolget)

        except KeyError:

            LOGERROR('LC for neighbor: %s (target object: %s) does not '
                     'have one or more of the required columns: %s, '
                     'skipping...' %
                     (objectid, checkplotdict['objectid'],
                      ', '.join([timecol, magcol, errcol])))
            continue

        # filter the input times, mags, errs; do sigclipping and normalization
        stimes, smags, serrs = sigclip_magseries(times,
                                                 mags,
                                                 errs,
                                                 magsarefluxes=magsarefluxes,
                                                 sigclip=4.0)

        # normalize here if not using special normalization
        if normfunc is None:
            ntimes, nmags = normalize_magseries(
                stimes, smags,
                magsarefluxes=magsarefluxes
            )
            xtimes, xmags, xerrs = ntimes, nmags, serrs
        else:
            xtimes, xmags, xerrs = stimes, smags, serrs


        # check if this neighbor has enough finite points in its LC
        # fail early if not enough light curve points
        if ((xtimes is None) or (xmags is None) or (xerrs is None) or
            (xtimes.size < 49) or (xmags.size < 49) or (xerrs.size < 49)):

            LOGERROR("one or more of times, mags, errs appear to be None "
                     "after sig-clipping. are the measurements all nan? "
                     "can't make neighbor light curve plots "
                     "for target: %s, neighbor: %s, neighbor LC: %s" %
                     (checkplotdict['objectid'],
                      nbr['objectid'],
                      nbr['lcfpath']))
            continue

        #
        # now we can start doing stuff if everything checks out
        #

        # make an unphased mag-series plot
        nbrdict = _pkl_magseries_plot(xtimes,
                                      xmags,
                                      xerrs,
                                      magsarefluxes=magsarefluxes)
        # update the nbr
        nbr.update(nbrdict)

        # for each lspmethod in the checkplot, make a corresponding plot for
        # this neighbor

        # figure out the period finder methods present
        if 'pfmethods' in checkplotdict:
            pfmethods = checkplotdict['pfmethods']
        else:
            pfmethods = []
            for cpkey in checkplotdict:
                for pfkey in PFMETHODS:
                    if pfkey in cpkey:
                        pfmethods.append(pfkey)

        for lspt in pfmethods:

            # initialize this lspmethod entry
            nbr[lspt] = {}

            # we only care about the best period and its options
            operiod, oepoch = (checkplotdict[lspt][0]['period'],
                               checkplotdict[lspt][0]['epoch'])
            (ophasewrap, ophasesort, ophasebin,
             ominbinelems, oplotxlim) = (
                 checkplotdict[lspt][0]['phasewrap'],
                 checkplotdict[lspt][0]['phasesort'],
                 checkplotdict[lspt][0]['phasebin'],
                 checkplotdict[lspt][0]['minbinelems'],
                 checkplotdict[lspt][0]['plotxlim'],
            )

            # make the phasedlc plot for this period
            nbr = _pkl_phased_magseries_plot(
                nbr,
                lspt.split('-')[1],  # this splits '<pfindex>-<pfmethod>'
                0,
                xtimes, xmags, xerrs,
                operiod, oepoch,
                phasewrap=ophasewrap,
                phasesort=ophasesort,
                phasebin=ophasebin,
                minbinelems=ominbinelems,
                plotxlim=oplotxlim,
                magsarefluxes=magsarefluxes,
                verbose=verbose,
                override_pfmethod=lspt
            )

    # at this point, this neighbor's dict should be up to date with all
    # info, magseries plot, and all phased LC plots
    # return the updated checkplotdict
    return checkplotdict



########################
## RUNNING CHECKPLOTS ##
########################

def runcp(pfpickle,
          outdir,
          lcbasedir,
          fast_mode=False,
          lcfname=None,
          cprenorm=False,
          lclistpkl=None,
          nbrradiusarcsec=60.0,
          maxnumneighbors=5,
          makeneighborlcs=True,
          gaia_max_timeout=60.0,
          gaia_mirror=None,
          xmatchinfo=None,
          xmatchradiusarcsec=3.0,
          minobservations=99,
          sigclip=10.0,
          lcformat='hat-sql',
          lcformatdir=None,
          timecols=None,
          magcols=None,
          errcols=None,
          skipdone=False,
          done_callback=None,
          done_callback_args=None,
          done_callback_kwargs=None):
    '''This runs a checkplot for the given period-finding result pickle
    produced by runpf.

    Args
    ----

    `pfpickle` is the filename of the pickle created by lcproc.runpf. If this is
    None, the checkplot will be made anyway, but no phased LC information will
    be collected into the output checkplot pickle. This can be useful for just
    collecting GAIA and other external information and making LC plots for an
    object.

    `outdir` is the directory to which the output checkplot pickle will be
    written.

    `lcbasedir` is the base directory where the light curves are located.

    `fast_mode` tries to speed up hits to external services. If this is True,
    the following kwargs will be set for calls to checkplot.checkplot_pickle:

    skyview_timeout = 10.0
    skyview_retry_failed = False
    simbad_search = False
    dust_timeout = 10.0
    gaia_submit_timeout = 5.0
    gaia_max_timeout = 5.0
    gaia_submit_tries = 1
    complete_query_later = False

    `lcfname` is usually None because we get the LC filename from the
    pfpickle. If pfpickle is None, however, lcfname is used instead. It will
    also be used as an override if it's provided instead of whatever the lcfname
    in pfpickle is.

    `cprenorm` is True if the light curves should be renormalized by
    checkplot.checkplot_pickle. This is set to False by default because we do
    our own normalization in this function using the light curve's registered
    normalization function and pass the normalized times, mags, errs to the
    checkplot.checkplot_pickle function.

    `lclistpkl` is the name of a pickle or the actual dict produced by
    lcproc.make_lclist. This is used to gather neighbor information.

    `nbrradiusarcsec` is the maximum radius in arcsec around the object which
    will be searched for any neighbors in lclistpkl.

    `maxnumneighbors` is the maximum number of neighbors that will be processed.

    `xmatchinfo` is the pickle or the actual dict containing external catalog
    information for cross-matching.

    `xmatchradiusarcsec` is the maximum match distance in arcseconds for
    cross-matching.

    `minobservations` is the minimum number of observations required to process
    the light curve.

    `sigclip` is the sigma-clip to apply to the light curve.

    `lcformat` is a key from the LCFORM dict to use when reading the light
    curves.

    `timecols` is a list of time columns from the light curve to process.

    `magcols` is a list of mag columns from the light curve to process.

    `errcols` is a list of err columns from the light curve to process.

    `skipdone` indicates if this function will skip creating checkplots that
    already exist corresponding to the current objectid and magcol. If
    `skipdone` is set to True, this will be done.

    `done_callback` is used to provide a function to execute after the checkplot
    pickles are generated. This is useful if you want to stream the results of
    checkplot making to some other process, e.g. directly running an ingestion
    into an LCC-Server collection. The function will always get the list of the
    generated checkplot pickles as its first arg, and all of the kwargs for
    runcp in the kwargs dict. Additional args and kwargs can be provided by
    giving a list in the `done_callbacks_args` kwarg and a dict in the
    `done_callbacks_kwargs` kwarg.

    NOTE: the function you pass in here should be pickleable by normal Python if
    you want to use it with the parallel_cp and parallel_cp_lcdir functions
    below.

    Returns
    -------

    a list of checkplot pickle filenames with one element for each (timecol,
    magcol, errcol) combination provided in the default lcformat config or in
    the timecols, magcols, errcols kwargs.

    '''

    try:
        formatinfo = get_lcformat(lcformat,
                                  use_lcformat_dir=lcformatdir)
        if formatinfo:
            (fileglob, readerfunc,
             dtimecols, dmagcols, derrcols,
             magsarefluxes, normfunc) = formatinfo
        else:
            LOGERROR("can't figure out the light curve format")
            return None
    except Exception as e:
        LOGEXCEPTION("can't figure out the light curve format")
        return None

    if pfpickle is not None:

        if pfpickle.endswith('.gz'):
            infd = gzip.open(pfpickle,'rb')
        else:
            infd = open(pfpickle,'rb')

        pfresults = pickle.load(infd)

        infd.close()


    # override the default timecols, magcols, and errcols
    # using the ones provided to the function
    if timecols is None:
        timecols = dtimecols
    if magcols is None:
        magcols = dmagcols
    if errcols is None:
        errcols = derrcols

    if ((lcfname is not None or pfpickle is None) and os.path.exists(lcfname)):

        lcfpath = lcfname
        objectid = None

    else:

        if pfpickle is not None:

            objectid = pfresults['objectid']
            lcfbasename = pfresults['lcfbasename']
            lcfsearchpath = os.path.join(lcbasedir, lcfbasename)

            if os.path.exists(lcfsearchpath):
                lcfpath = lcfsearchpath

            elif lcfname is not None and os.path.exists(lcfname):
                lcfpath = lcfname

            else:
                LOGERROR('could not find light curve for '
                         'pfresult %s, objectid %s, '
                         'used search path: %s, lcfname kwarg: %s' %
                         (pfpickle, objectid, lcfsearchpath, lcfname))
                return None

        else:

            LOGERROR("no light curve provided and pfpickle is None, "
                     "can't continue")
            return None

    lcdict = readerfunc(lcfpath)

    # this should handle lists/tuples being returned by readerfunc
    # we assume that the first element is the actual lcdict
    # FIXME: figure out how to not need this assumption
    if ( (isinstance(lcdict, (list, tuple))) and
         (isinstance(lcdict[0], dict)) ):
        lcdict = lcdict[0]

    # get the object ID from the light curve if pfpickle is None or we used
    # lcfname directly
    if objectid is None:

        if 'objectid' in lcdict:
            objectid = lcdict['objectid']
        elif ('objectid' in lcdict['objectinfo'] and
              lcdict['objectinfo']['objectid']):
            objectid = lcdict['objectinfo']['objectid']
        elif 'hatid' in lcdict['objectinfo'] and lcdict['objectinfo']['hatid']:
            objectid = lcdict['objectinfo']['hatid']
        else:
            objectid = uuid.uuid4().hex[:5]
            LOGWARNING('no objectid found for this object, '
                       'generated a random one: %s' % objectid)

    # normalize using the special function if specified
    if normfunc is not None:
        lcdict = normfunc(lcdict)

    cpfs = []

    for tcol, mcol, ecol in zip(timecols, magcols, errcols):

        # dereference the columns and get them from the lcdict
        if '.' in tcol:
            tcolget = tcol.split('.')
        else:
            tcolget = [tcol]
        times = dict_get(lcdict, tcolget)

        if '.' in mcol:
            mcolget = mcol.split('.')
        else:
            mcolget = [mcol]
        mags = dict_get(lcdict, mcolget)

        if '.' in ecol:
            ecolget = ecol.split('.')
        else:
            ecolget = [ecol]
        errs = dict_get(lcdict, ecolget)

        # get all the period-finder results from this magcol
        if pfpickle is not None:

            if 'pfmethods' in pfresults[mcol]:
                pflist = [
                    pfresults[mcol][x] for x in
                    pfresults[mcol]['pfmethods'] if
                    len(pfresults[mcol][x].keys()) > 0
                ]
            else:
                pflist = []
                for pfm in PFMETHODS:
                    if (pfm in pfresults[mcol] and
                        len(pfresults[mcol][pfm].keys()) > 0):
                        pflist.append(pfresults[mcol][pfm])

        # special case of generating a checkplot with no phased LCs
        else:
            pflist = []

        # generate the output filename
        outfile = os.path.join(outdir,
                               'checkplot-%s-%s.pkl' % (
                                   squeeze(objectid).replace(' ','-'),
                                   mcol
                               ))

        if skipdone and os.path.exists(outfile):
            LOGWARNING('skipdone = True and '
                       'checkplot for this objectid/magcol combination '
                       'exists already: %s, skipping...' % outfile)
            return outfile

        # make sure the checkplot has a valid objectid
        if 'objectid' not in lcdict['objectinfo']:
            lcdict['objectinfo']['objectid'] = objectid

        # normalize here if not using special normalization
        if normfunc is None:
            ntimes, nmags = normalize_magseries(
                times, mags,
                magsarefluxes=magsarefluxes
            )
            xtimes, xmags, xerrs = ntimes, nmags, errs
        else:
            xtimes, xmags, xerrs = times, mags, errs

        # generate the checkplotdict
        cpd = checkplot_dict(
            pflist,
            xtimes, xmags, xerrs,
            objectinfo=lcdict['objectinfo'],
            gaia_max_timeout=gaia_max_timeout,
            gaia_mirror=gaia_mirror,
            lclistpkl=lclistpkl,
            nbrradiusarcsec=nbrradiusarcsec,
            maxnumneighbors=maxnumneighbors,
            xmatchinfo=xmatchinfo,
            xmatchradiusarcsec=xmatchradiusarcsec,
            sigclip=sigclip,
            mindet=minobservations,
            verbose=False,
            fast_mode=fast_mode,
            magsarefluxes=magsarefluxes,
            normto=cprenorm  # we've done the renormalization already, so this
                             # should be False by default. just messes up the
                             # plots otherwise, destroying LPVs in particular
        )

        if makeneighborlcs:

            # include any neighbor information as well
            cpdupdated = update_checkplotdict_nbrlcs(
                cpd,
                tcol, mcol, ecol,
                lcformat=lcformat,
                verbose=False
            )

        else:

            cpdupdated = cpd

        # write the update checkplot dict to disk
        cpf = _write_checkplot_picklefile(
            cpdupdated,
            outfile=outfile,
            protocol=pickle.HIGHEST_PROTOCOL,
            outgzip=False
        )

        cpfs.append(cpf)

    #
    # done with checkplot making
    #

    LOGINFO('done with %s -> %s' % (objectid, repr(cpfs)))
    if done_callback is not None:

        if (done_callback_args is not None and
            isinstance(done_callback_args,list)):
            done_callback_args = tuple([cpfs] + done_callback_args)

        else:
            done_callback_args = (cpfs,)

        if (done_callback_kwargs is not None and
            isinstance(done_callback_kwargs, dict)):
            done_callback_kwargs.update(dict(
                fast_mode=fast_mode,
                lcfname=lcfname,
                cprenorm=cprenorm,
                lclistpkl=lclistpkl,
                nbrradiusarcsec=nbrradiusarcsec,
                maxnumneighbors=maxnumneighbors,
                gaia_max_timeout=gaia_max_timeout,
                gaia_mirror=gaia_mirror,
                xmatchinfo=xmatchinfo,
                xmatchradiusarcsec=xmatchradiusarcsec,
                minobservations=minobservations,
                sigclip=sigclip,
                lcformat=lcformat,
                fileglob=fileglob,
                readerfunc=readerfunc,
                normfunc=normfunc,
                magsarefluxes=magsarefluxes,
                timecols=timecols,
                magcols=magcols,
                errcols=errcols,
                skipdone=skipdone,
            ))

        else:
            done_callback_kwargs = dict(
                fast_mode=fast_mode,
                lcfname=lcfname,
                cprenorm=cprenorm,
                lclistpkl=lclistpkl,
                nbrradiusarcsec=nbrradiusarcsec,
                maxnumneighbors=maxnumneighbors,
                gaia_max_timeout=gaia_max_timeout,
                gaia_mirror=gaia_mirror,
                xmatchinfo=xmatchinfo,
                xmatchradiusarcsec=xmatchradiusarcsec,
                minobservations=minobservations,
                sigclip=sigclip,
                lcformat=lcformat,
                fileglob=fileglob,
                readerfunc=readerfunc,
                normfunc=normfunc,
                magsarefluxes=magsarefluxes,
                timecols=timecols,
                magcols=magcols,
                errcols=errcols,
                skipdone=skipdone,
            )

        # fire the callback
        try:
            done_callback(*done_callback_args, **done_callback_kwargs)
            LOGINFO('callback fired successfully for %r' % cpfs)
        except Exception as e:
            LOGEXCEPTION('callback function failed for %r' % cpfs)

    # at the end, return the list of checkplot files generated
    return cpfs



def runcp_worker(task):
    '''
    This is the worker for running checkplots.

    '''

    pfpickle, outdir, lcbasedir, kwargs = task

    try:

        return runcp(pfpickle, outdir, lcbasedir, **kwargs)

    except Exception as e:

        LOGEXCEPTION(' could not make checkplots for %s: %s' % (pfpickle, e))
        return None



def parallel_cp(pfpicklelist,
                outdir,
                lcbasedir,
                fast_mode=False,
                lcfnamelist=None,
                cprenorm=False,
                lclistpkl=None,
                gaia_max_timeout=60.0,
                gaia_mirror=None,
                nbrradiusarcsec=60.0,
                maxnumneighbors=5,
                makeneighborlcs=True,
                xmatchinfo=None,
                xmatchradiusarcsec=3.0,
                sigclip=10.0,
                minobservations=99,
                liststartindex=None,
                maxobjects=None,
                lcformat='hat-sql',
                lcformatdir=None,
                timecols=None,
                magcols=None,
                errcols=None,
                skipdone=False,
                nworkers=NCPUS,
                done_callback=None,
                done_callback_args=None,
                done_callback_kwargs=None):
    '''This drives the parallel execution of runcp for a list of periodfinding
    result pickles.

    '''

    # work around the Darwin segfault after fork if no network activity in
    # main thread bug: https://bugs.python.org/issue30385#msg293958
    if sys.platform == 'darwin':
        import requests
        requests.get('http://captive.apple.com/hotspot-detect.html')

    if not os.path.exists(outdir):
        os.mkdir(outdir)

    # handle the start and end indices
    if (liststartindex is not None) and (maxobjects is None):
        pfpicklelist = pfpicklelist[liststartindex:]
        if lcfnamelist is not None:
            lcfnamelist = lcfnamelist[liststartindex:]

    elif (liststartindex is None) and (maxobjects is not None):
        pfpicklelist = pfpicklelist[:maxobjects]
        if lcfnamelist is not None:
            lcfnamelist = lcfnamelist[:maxobjects]

    elif (liststartindex is not None) and (maxobjects is not None):
        pfpicklelist = (
            pfpicklelist[liststartindex:liststartindex+maxobjects]
        )
        if lcfnamelist is not None:
            lcfnamelist = lcfnamelist[liststartindex:liststartindex+maxobjects]

    # if the lcfnamelist is not provided, create a dummy
    if lcfnamelist is None:
        lcfnamelist = [None]*len(pfpicklelist)

    tasklist = [(x, outdir, lcbasedir,
                 {'lcformat':lcformat,
                  'lcformatdir':lcformatdir,
                  'lcfname':y,
                  'timecols':timecols,
                  'magcols':magcols,
                  'errcols':errcols,
                  'lclistpkl':lclistpkl,
                  'gaia_max_timeout':gaia_max_timeout,
                  'gaia_mirror':gaia_mirror,
                  'nbrradiusarcsec':nbrradiusarcsec,
                  'maxnumneighbors':maxnumneighbors,
                  'makeneighborlcs':makeneighborlcs,
                  'xmatchinfo':xmatchinfo,
                  'xmatchradiusarcsec':xmatchradiusarcsec,
                  'sigclip':sigclip,
                  'minobservations':minobservations,
                  'skipdone':skipdone,
                  'cprenorm':cprenorm,
                  'fast_mode':fast_mode,
                  'done_callback':done_callback,
                  'done_callback_args':done_callback_args,
                  'done_callback_kwargs':done_callback_kwargs}) for
                x,y in zip(pfpicklelist, lcfnamelist)]

    resultfutures = []
    results = []

    with ProcessPoolExecutor(max_workers=nworkers) as executor:
        resultfutures = executor.map(runcp_worker, tasklist)

    results = [x for x in resultfutures]

    executor.shutdown()
    return results



def parallel_cp_pfdir(pfpickledir,
                      outdir,
                      lcbasedir,
                      fast_mode=False,
                      cprenorm=False,
                      lclistpkl=None,
                      gaia_max_timeout=60.0,
                      gaia_mirror=None,
                      nbrradiusarcsec=60.0,
                      maxnumneighbors=5,
                      makeneighborlcs=True,
                      xmatchinfo=None,
                      xmatchradiusarcsec=3.0,
                      sigclip=10.0,
                      minobservations=99,
                      maxobjects=None,
                      pfpickleglob='periodfinding-*.pkl*',
                      lcformat='hat-sql',
                      lcformatdir=None,
                      timecols=None,
                      magcols=None,
                      errcols=None,
                      skipdone=False,
                      nworkers=32,
                      done_callback=None,
                      done_callback_args=None,
                      done_callback_kwargs=None):

    '''This drives the parallel execution of runcp for a directory of
    periodfinding pickles.

    '''

    pfpicklelist = sorted(glob.glob(os.path.join(pfpickledir, pfpickleglob)))

    LOGINFO('found %s period-finding pickles, running cp...' %
            len(pfpicklelist))

    return parallel_cp(pfpicklelist,
                       outdir,
                       lcbasedir,
                       fast_mode=fast_mode,
                       lclistpkl=lclistpkl,
                       nbrradiusarcsec=nbrradiusarcsec,
                       gaia_max_timeout=gaia_max_timeout,
                       gaia_mirror=gaia_mirror,
                       maxnumneighbors=maxnumneighbors,
                       makeneighborlcs=makeneighborlcs,
                       xmatchinfo=xmatchinfo,
                       xmatchradiusarcsec=xmatchradiusarcsec,
                       sigclip=sigclip,
                       minobservations=minobservations,
                       cprenorm=cprenorm,
                       maxobjects=maxobjects,
                       lcformat=lcformat,
                       lcformatdir=lcformatdir,
                       timecols=timecols,
                       magcols=magcols,
                       errcols=errcols,
                       skipdone=skipdone,
                       nworkers=nworkers,
                       done_callback=done_callback,
                       done_callback_args=done_callback_args,
                       done_callback_kwargs=done_callback_kwargs)
