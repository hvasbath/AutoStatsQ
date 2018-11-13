import os, sys
import numpy as num
import datetime
import logging
import linecache
import gc
import math
import argparse

import matplotlib.pyplot as plt
from pyrocko import util, model, orthodrome, pile, trace, io
from pyrocko import cake, gf
from pyrocko.client import catalog, fdsn
from pyrocko.io import stationxml
from pyrocko.fdsn import station as fs

#from .gainfactors import *
from . import gainfactors as gainf 
from .catalog import subset_events_dist_cat, subset_events_dist_evlist 
from .catalogplots import *
from .gainplots import plot_median_gain_map_from_file
from . import freq_psd as fp
from . import orient
from .config_settings_defaults import generate_default_config
from .config import GeneralSettings, CatalogConfig, ArrTConfig,\
MetaDataDownloadConfig, RestDownRotConfig, SynthDataConfig,\
GainfactorsConfig, PSDConfig, OrientConfig, maps, AutoStatsQConfig
from .calc_ttt import *

#st_liste_check = ['CHVC','CKRC','DPC','GOPC','HSKC','JAVC','KRLC','KRUC',
#                  'MORC','NKC','OSTC','PBCC','PRA','PVCC','TREC','UPC',
#                  'VRAC','GRA1','GRFO','BNI']
#st_liste_check = ['GOLS', 'PDKS', 'VNDS', 'GRFO']
#net_check = ['CR'] #CH

#st_liste_check = ['RNON', 'FFB1','AGO','CARF','CIEL','COLF','ILLK','ILLF',
#                  'LBM','LEMB','OGS2','OGS3','PLDF','PLYF','PRIMA','SALF','SZBH',
#                  'WALT','IMI','MAGO','NDIM','VIE']

#st_liste_check = ['NICK', 'LAGB']

'''
Quality control of array stations

0.  Read station lists
    - needs comma-spread list of stations with information on
      net, stat, lat, lom, elev, depth

1.  Catalog search for teleseismic events
    - queries the geofon catalog
    - opt. plots of catalog statistics + map

2.  Subset of events for quality control
    - search for subset of events that will be used for quality control
    - opt. plot: map

3.  Download data and metadata
    - query sites defined in settings, fdsn-download
    - both optional, local data can be used.

4.  Data preparation: restitution of data

5.  Rotation NE --> RT

6.  Synthetic data

7.  Calc. Gain factors (relative)
    - returns list of mean gain factor of each station rel. to ref. station +
      list of gain factors of all stations/events rel. to ref. station
    - opt. plot: x-y plot of all gain factors

8. PSDs
    - comp. synth and obs. PSDs (>30 min twd)
    - providing ranges of flat ratio for MT inv

9. Rayleigh wave polarization analysis for orientation
'''


def main(): 
  
    # Any event with "bad" data to be excluded?
    exclude_event = ['2017-11-04 09:00:19.000']


    # Command line input handling
    parser = argparse.ArgumentParser(
                    description='')
    parser.add_argument('--config')
    parser.add_argument('--run')
    parser.add_argument('--generate_config')
    args = parser.parse_args()


    # Generate a (template) config file:
    if args.generate_config:
        fn_config = 'AutoStatsQ_settings.config'
        if os.path.exists('AutoStatsQ_settings.config'):
            print('file exists: %s' % fn_config)

        config = generate_default_config()

        config.dump(filename=fn_config)
        print('created a fresh config file "%s"' % fn_config)


    # run AutoStatsQ
    if args.run:

        # read existing config file:

        gensettings, catalogconf, arrTconf, ct, metaDataconf, RestDownconf,\
        synthsconf, gainfconf, psdsconf, orientconf, maps =\
        AutoStatsQConfig.load(filename=args.config).Settings

        data_dir = gensettings.data_dir
        os.makedirs(data_dir+'./results', exist_ok=True)

        sites = metaDataconf.sites

        ''' 0. Read station lists '''
        st_lats = []
        st_lons = []
        ns = []
        all_stations = []

        for stat_list in gensettings.list_station_lists: # depth fehlt gerade
            if stat_list.endswith('.csv'):
               with open(stat_list, 'r') as f:
                    for line in f.readlines():
                        n, s, lat, lon, elev, d = line.strip().split(',')
                        st_lats.append(float(lat))
                        st_lons.append(float(lon))
                        ns.append((n, s))
                        all_stations.append(model.Station(network=n, station=s,
                                                          lat=float(lat), lon=float(lon),
                                                          elevation=float(elev), depth=d))
            elif stat_list.endswith('.xml'):
                zs = stationxml.load_xml(filename=stat_list)
                for net in zs.network_list:
                    for stat in net.station_list:
                        st_lats.append(float(stat.latitude.value))
                        st_lons.append(float(stat.longitude.value))
                        ns.append((net.code, stat.code))
                        all_stations.append(model.Station(network=net.code,
                                            station=stat.code,
                                            lat=float(stat.latitude.value),
                                            lon=float(stat.longitude.value),
                                            elevation=float(stat.elevation.value)))

            else:
              print('Station file extension not known: %s' % stat_list)

        print('stations:', len(ns))

        ##### FOR SHORT TESTING
        '''
        all_stations = all_stations[161:175]
        ns = ns[161:175]
        st_lats = st_lats[161:175]
        st_lons = st_lons[161:175]

        print(ns)
        print('stations:', len(ns))
        '''
        #####


        ''' 1. Catalog search for teleseismic events '''

        tmin = util.ctimegm(catalogconf.tmin_str)
        tmax = util.ctimegm(catalogconf.tmax_str)
        os.makedirs(data_dir+'./results/catalog', exist_ok=True)

        if catalogconf.search_events is True:

            geofon = catalog.GlobalCMT()
            event_names = geofon.get_event_names(
                time_range=(tmin, tmax),
                magmin=catalogconf.min_mag)
            ev_catalog = []

            for ev_name in event_names:
                ev_catalog.append(geofon.get_event(ev_name))

            print('%s events found.' % (str(len(ev_catalog))))
            model.dump_events(ev_catalog, data_dir+'results/catalog/catalog_Mgr'+str(catalogconf.min_mag)+'.txt')
            print('length catalog:', len(ev_catalog))

        if catalogconf.use_local_catalog is True:

            if catalogconf.subset_of_local_catalog is False:
                ev_catalog = model.load_events(catalogconf.catalog_fn)

            if catalogconf.subset_of_local_catalog is True:
                ev_catalog = subset_events_dist_cat(catalogconf.catalog_fn,
                                             catalogconf.min_mag,
                                             catalogconf.max_mag,
                                             catalogconf.tmin_str,
                                             catalogconf.tmax_str,
                                             catalogconf.mid_point[0],
                                             catalogconf.mid_point[1],
                                             catalogconf.min_dist_km,
                                             catalogconf.max_dist_km)

            print('length catalog:', len(ev_catalog))


        ''' 2. Subset of events for quality control'''

        subsets_events = {}
        no_bins = int(360/catalogconf.wedges_width)

        for d, val in catalogconf.depth_options.items():

            if not catalogconf.use_local_subsets is True:

                ev_cat = subset_events_dist_evlist(ev_catalog,
                                                   catalogconf.min_mag,
                                                   catalogconf.max_mag,
                                                   catalogconf.tmin_str,
                                                   catalogconf.tmax_str,
                                                   catalogconf.mid_point[0],
                                                   catalogconf.mid_point[1],
                                                   val[0],
                                                   val[1],
                                                   catalogconf.min_dist_km,
                                                   catalogconf.max_dist_km)


                dist_array = num.empty((len(ev_cat), len(ns)))
                bazi_array = num.empty((len(ev_cat), len(ns)))
                bazi_mp_array = num.empty((len(ev_cat)))

                for i_ev, ev in enumerate(ev_cat):
                    dist_array[i_ev, :] = [float(orthodrome.distance_accurate50m_numpy(
                                          ev.lat, ev.lon, lat, lon))
                                          for (lat, lon) in zip(st_lats, st_lons)]

                    bazi_array[i_ev, :] = [orthodrome.azibazi(ev.lat, ev.lon, lat, lon)[1]
                                           for (lat, lon) in zip(st_lats, st_lons)]

                    bazi_mp_array[i_ev] = orthodrome.azibazi(ev.lat, ev.lon,
                                                             catalogconf.mid_point[0],
                                                             catalogconf.mid_point[1])[1]

                if catalogconf.plot_catalog_all is True:
                    os.makedirs(data_dir+'results/catalog/', exist_ok=True)
                    fn = '%sresults/catalog/catalog_global_Mgr%s_%s-%s_%s.png' % (data_dir,
                         str(catalogconf.min_mag), catalogconf.tmin_str[0:10],
                         catalogconf.tmax_str[0:10], d)

                    gmtplot_catalog_azimuthal(ev_cat, catalogconf.mid_point,
                                              catalogconf.dist, fn,
                                              catalogconf.wedges_width)
                
                wedges_array = num.floor(bazi_array / catalogconf.wedges_width)

                # hist based on chosen mid_point of array
                wedges_array_mp = num.floor(bazi_mp_array / catalogconf.wedges_width)
                mean_wedges_mp = num.where(wedges_array_mp < 0,
                                           no_bins+wedges_array_mp,
                                           wedges_array_mp)
                bins_hist = [a for a in range(no_bins+1)]
                hist, bin_edges = num.histogram(mean_wedges_mp, bins=bins_hist)


                if catalogconf.plot_hist_wedges is True:
                    plot_catalog_hist(ev_cat, dist_array, mean_wedges_mp,
                                      bins_hist, data_dir, catalogconf.min_mag, d,
                                      catalogconf.wedges_width, no_bins)

                if catalogconf.plot_dist_vs_magn is True:
                    plot_distmagn(dist_array, ev_cat, data_dir, d)


                # find 'best' subset of catalog events
                subset_catalog = []

                for bin_nr in range(no_bins):
                    # get indices of all events in current bin:
                    bin_ev_ind = num.argwhere(mean_wedges_mp == bin_nr)

                    if len(bin_ev_ind) == 0:
                        print('no event for %d - %d deg' % (bin_nr*catalogconf.wedges_width,
                             (bin_nr+1)*catalogconf.wedges_width))

                    if len(bin_ev_ind) == 1:
                        subset_catalog.append(ev_cat[int(bin_ev_ind[0])])

                    if len(bin_ev_ind) > 1:
                        # choose event
                        # if around it bins with no event choose more to that side,
                        # if on both sides
                        # no events if possible two events at both bin margins

                        if bin_nr != 0 and bin_nr != no_bins-1 and\
                          hist[bin_nr-1] == 0 and hist[bin_nr+1] == 0:
                            # choose min und max bazi for better azimuthal coverage
                            min_bazi_ev_ind = bin_ev_ind[num.argmin(bazi_mp_array[bin_ev_ind])][0]
                            max_bazi_ev_ind = bin_ev_ind[num.argmax(bazi_mp_array[bin_ev_ind])][0]
                            subset_catalog.append(ev_cat[min_bazi_ev_ind])
                            subset_catalog.append(ev_cat[max_bazi_ev_ind])

                        elif bin_nr != 0 and hist[bin_nr-1] == 0:
                                # choose one which is more to that side
                            ev_ind_next = bin_ev_ind[
                                                     num.argsort(
                                                                bazi_mp_array[bin_ev_ind],
                                                                axis=0)[0]][0][0]
                            subset_catalog.append(ev_cat[ev_ind_next])

                        elif bin_nr != no_bins-1 and hist[bin_nr+1] == 0:
                                # choose one more to that side
                            ev_ind_next = bin_ev_ind[num.argsort(
                                                                bazi_mp_array[bin_ev_ind],
                                                                axis=0)
                                                     [len(bin_ev_ind)-1]][0][0]
                            subset_catalog.append(ev_cat[ev_ind_next])

                        else:
                            if catalogconf.median_ev_in_bin is True:
                                # only in middle if uniformly distributed eqs in bin!
                                median_ev_ind = bin_ev_ind[num.argsort(
                                                                  bazi_mp_array[bin_ev_ind],
                                                                  axis=0)
                                                           [len(bin_ev_ind)//2]][0][0]
                                subset_catalog.append(ev_cat[median_ev_ind])

                            # if catalogconf.weighted_magn_baz_ev is True:
                            #     # alternatively weighting with magnitude:
                            #     # weighting of magnitude
                            #     mags = [ev.magnitude for ev in [ev_catalog[i_ev[0]]
                            #             for i_ev in bin_ev_ind]]
                            #     max_mag = max(mags)
                            #     min_mag = min(mags)
                            #     W_mag = (num.max(bazi_mp_array[bin_ev_ind]) -
                            #              num.min(bazi_mp_array[bin_ev_ind])) /\
                            #             (max_mag - min_mag) + 0.2
                            #     d_to_opt = []
                            #     baz_op = (bin_nr+1) * catalogconf.wedges_width - catalogconf.wedges_width/2.
                            #     if baz_op > 180:
                            #         baz_op = baz_op - 360
                            #     for ii in bin_ev_ind:
                            #         d_to_opt.append(num.sqrt(
                            #                         W_mag *
                            #                         num.square(max(mags) -
                            #                         ev_catalog[ii[0]].magnitude) +
                            #                         num.square(abs(baz_op -
                            #                         bazi_mp_array[ii]))))

                            #     i_best_ev = bin_ev_ind[num.argmin(d_to_opt)]
                            #     subset_catalog.append(ev_catalog[i_best_ev[0]])
                
                            '''
                            print('best and median event index',
                                  i_best_ev, median_ev_ind,
                                  baz_op, bazi_mp_array[i_best_ev],
                                  bazi_mp_array[median_ev_ind],
                                  catalog[i_best_ev].magnitude,
                                  catalog[median_ev_ind].magnitude)
                            '''
              
                print('Subset of %d events was generated for %s.' % (len(subset_catalog), d))
                
                # sort subset catalog by time:
                subset_catalog.sort(key=lambda x: x.time)

                # append to subsets-dict
                subsets_events[d] = subset_catalog

                model.dump_events(subset_catalog, '%sresults/catalog/catalog_Mgr%s_%s.txt' 
                                  %(data_dir, str(catalogconf.min_mag), d))
                # print([(util.time_to_str(ev.time), ev.magnitude, ev.depth)

            else:
                subset_catalog = model.load_events(catalogconf.subset_fns[d])
                subsets_events[d] = subset_catalog

            if catalogconf.plot_catalog_subset is True:
                '''
                plot all events of catalog
                '''
                os.makedirs(data_dir+'results/catalog/', exist_ok=True)
                _tmin = util.time_to_str(min([ev.time for ev in subset_catalog]))
                _tmax = util.time_to_str(max([ev.time for ev in subset_catalog]))                
                fn = '%sresults/catalog/catalog_global_Mgr%s_%s-%s_%s_subset.pdf' % (data_dir, 
                     str(catalogconf.min_mag),
                     _tmin[0:10], _tmax[0:10], d)

                gmtplot_catalog_azimuthal(subset_catalog, catalogconf.mid_point, 
                                               catalogconf.dist, fn, catalogconf.wedges_width)

            if exclude_event != []:
                new_subset_catalog = []
                for ev in subset_catalog:
                    if util.time_to_str(ev.time) not in exclude_event:
                        new_subset_catalog.append(ev)
                subset_catalog = new_subset_catalog




            ''' 2.1 Calculate arrival times for all event/station pairs '''
            # Method a) using cake
            arrT_array = None
            arrT_R_array = None
            if arrTconf.calc_first_arr_t is True:
                data_dir = gensettings.data_dir
                os.makedirs(data_dir+'ttt', exist_ok=True)            
                dist_array_sub = num.empty((len(subset_catalog), len(ns)))

                for i_ev, ev in enumerate(subset_catalog):
                    dist_array_sub[i_ev, :] = [float(orthodrome.distance_accurate50m_numpy(
                                          ev.lat, ev.lon, lat, lon))
                                          for (lat, lon) in zip(st_lats, st_lons)]

                arrT_array = num.empty((dist_array_sub.shape))
                depths = [ev.depth for ev in subset_catalog]
                vmodel = cake.load_model('prem-no-ocean.f')
                phases = [cake.PhaseDef(pid) for pid in arrTconf.phase_select.split('|')]

                for i_ev, ev in enumerate(subset_catalog):
                    ds = depths[i_ev]
                    print(' calculating arr times for:', util.time_to_str(ev.time))

                    for i_st in range(len(ns)):
                        dist = dist_array_sub[i_ev, i_st]
                        arrivals = vmodel.arrivals(distances=[dist*cake.m2d],
                                                  phases=phases,
                                                  zstart=ds)

                        min_t = min(arrivals, key=lambda x: x.t).t

                        arrT_array[i_ev, i_st] = ev.time + min_t

                num.save('%sttt/ArrivalTimes_%s' % (data_dir, d), arrT_array)

            if arrTconf.calc_est_R is True:
                print('computing R arrival times')
                data_dir = gensettings.data_dir
                os.makedirs(data_dir+'ttt', exist_ok=True)            
                dist_array_sub = num.empty((len(subset_catalog), len(ns)))

                for i_ev, ev in enumerate(subset_catalog):
                    dist_array_sub[i_ev, :] = [float(orthodrome.distance_accurate50m_numpy(
                                          ev.lat, ev.lon, lat, lon))
                                          for (lat, lon) in zip(st_lats, st_lons)]

                arrT_R_array = num.empty((dist_array_sub.shape))

                for i_ev, ev in enumerate(subset_catalog):
                    for i_st in range(len(ns)):
                        dist = dist_array_sub[i_ev, i_st]  # m
                        #print('------')
                        #print('d', dist)
                        #print('dt', dist/4000.)
                        #print('t', ev.time)
                        #print('new t', ev.time + dist/4000.)
                        arrT_R_array[i_ev, i_st] = ev.time + dist/4000.

                num.save('%sttt/ArrivalTimes_estR_%s' % (data_dir, d), arrT_R_array)

            # Method b) interpolating from fomosto travel time tables
            arrT_array = None
            if ct.calc_ttt is True:
                data_dir = gensettings.data_dir
                os.makedirs(data_dir+'ttt', exist_ok=True)
                arrT_array_ttt = num.empty((len(subset_catalog), len(ns)))

                n = len(subset_catalog)
                # array n*3 (r_depth, s_depth, distance) in m
                coords = num.zeros((n,3))
                # coords[:,0] receiver_depths are zero
                coords[:,1] = [ev.depth for ev in subset_catalog]
                print('depth_min', num.min(coords[:,1]))
                print('depth_max', num.max(coords[:,1]))

                origin_times = num.asarray([ev.time for ev in subset_catalog])

                for i_st, (lat, lon) in enumerate(zip(st_lats, st_lons)):
                    coords[:,2] = [float(orthodrome.distance_accurate50m_numpy(
                                          ev.lat, ev.lon, lat, lon))
                                          for ev in subset_catalog]

                    intp_times = num.asarray(get_ttt(ct, coords, val))

                    arrT_array_ttt[:, i_st] = origin_times + intp_times
                    if i_st ==2:
                        print(coords[:])
                        print(arrT_array_ttt[:, i_st])

                num.save('%sttt/ArrivalTimes_%s' % (data_dir, d), arrT_array_ttt)
                # print(arrT_array_ttt[0:3,0:50])


        ''' 3. Download data and metadata '''
        data_pile = None
        # token = open(metaDataconf.token, 'rb').read()

        if metaDataconf.download_data is True:   ### clean up!

            for subset_catalog in subsets_events.values():

                for ev in subset_catalog:
                    ev_t_str = util.time_to_str(ev.time)
                    ev_t = datetime.datetime.strptime(ev_t_str, "%Y-%m-%d %H:%M:%S.%f")
                    dt_s = datetime.timedelta(hours=metaDataconf.dt_start)
                    dt_e = datetime.timedelta(hours=metaDataconf.dt_end)
                    t_start = util.str_to_time(str(ev_t - dt_s),
                                               format='%Y-%m-%d %H:%M:%S.OPTFRAC')
                    t_end = util.str_to_time(str(ev_t + dt_e),
                                             format='%Y-%m-%d %H:%M:%S.OPTFRAC')
                    ev_t_str = ev_t_str.replace(' ', '_')
                    ev_dir = ev_t_str + '/'
                    dir_make = data_dir + ev_dir
                    os.makedirs(dir_make, exist_ok=True)

                    for ns_now in ns:
                        #if ns_now[1] not in st_liste_check:
                        #    continue
                        selection = [(ns_now[0], ns_now[1], '*',
                                      metaDataconf.components_download,
                                      t_start, t_end)]
                        print(selection)

                        for site in sites:

                            if site in metaDataconf.token:
                                token = open(metaDataconf.token[site], 'rb').read()
                            else:
                                token = None
                            try:
                                if token:
                                    request_waveform = fdsn.dataselect(site=site,
                                                                       selection=selection,
                                                                       token=token)
                                else:
                                    request_waveform = fdsn.dataselect(site=site,
                                                                       selection=selection)

                                mseed_fn = data_dir + ev_dir + ns_now[0] + '_' +\
                                           ns_now[1] + '_' +\
                                           ev_t_str + site + 'tr.mseed'

                                with open(mseed_fn, 'wb') as wffile:
                                    wffile.write(request_waveform.read())

                            except fdsn.EmptyResult:
                                print(ns_now, 'no data', site)

                            except:
                                print('exception unknown', ns_now)

                            else:
                                print(ns_now, 'data downloaded', site)
                                break


        if metaDataconf.download_metadata is True:
            print('Downloading metadata')

            cat_tmin = min([ev.time for ev in subset_catalog for k, subset_catalog in subsets_events.items()])
            cat_tmax = max([ev.time for ev in subset_catalog for k, subset_catalog in subsets_events.items()])

            # one file for all events
            selection = []
            for ns_now in ns:
                selection.append((ns_now[0], ns_now[1], '*',
                                  metaDataconf.components_download,
                                  cat_tmin, cat_tmax))

            meta_fn = data_dir + 'Resp_all'

            for site in sites:
                # This sometimes does not work properly, why? Further testing?...
                print(site)
                #try:
                request_response = fdsn.station(
                        site=site, selection=selection, level='response')
                request_response.dump_xml(filename=meta_fn + '_' +
                                              str(site) + '.xml')
                #except:
                #    print('no metadata at all', site, selection[1])


        ''' 4. Data preparation: restitution of data '''
        if RestDownconf.rest_data is True:
            print('Starting restitution of data.')
            responses = []

            if metaDataconf.local_metadata != []:
                for file in metaDataconf.local_metadata:
                    responses.append(stationxml.load_xml(filename=file))
            
            if metaDataconf.use_downmeta is True:
                for site in sites:
                    stations_fn = data_dir + 'Resp_all_' + str(site) + '.xml'
                    responses.append(stationxml.load_xml(filename=stations_fn))

            i_resp = len(responses)
            print(i_resp)

            for key, subset_catalog in subsets_events.items(): 

                for ev in subset_catalog:
                    print(util.time_to_str(ev.time))
                    ev_t_str = util.time_to_str(ev.time).replace(' ', '_')

                    #stations_fn = data_dir + ev_t_str + '_resp_geofon.xml'
                    #response = stationxml.load_xml(filename=stations_fn)
                    p = pile.make_pile(paths=data_dir+ev_t_str, show_progress=False)
                    dir_make = data_dir + 'rest/' + ev_t_str
                    os.makedirs(dir_make, exist_ok=True)
                    transf_taper = 1/min(RestDownconf.freqlim)

                    for st in all_stations:
                        #print(st.station)
                        #if st.station not in st_liste_check:
                        #    continue
                        #if st.network not in net_check:
                        #    continue                        
                        nsl = st.nsl()
                        trs = p.all(
                            trace_selector=lambda tr: tr.nslc_id[:2] == nsl[:2])

                        if not trs and metaDataconf.local_data:
                          p = pile.make_pile(paths=metaDataconf.local_data, show_progress=False,
                            regex=st.station)                       
                          tmin=ev.time+metaDataconf.dt_start*3600
                          tmax=ev.time+metaDataconf.dt_end*3600
                          trs = p.all(
                            tmin=tmin,
                            tmax=tmax)

                        if trs:
                            comps = [tr.channel for tr in trs]
                            if 'HHZ' in comps and 'HHN' in comps and 'HHE' in comps:
                                trs = [tr for tr in trs if tr.channel in ['HHZ', 'HHN', 'HHE']]
                            elif 'HHZ' in comps and 'HH2' in comps and 'HH3' in comps:
                                trs = [tr for tr in trs if tr.channel in ['HHZ', 'HH2', 'HH3']]
                            elif 'HHZ' in comps and 'HH1' in comps and 'HH1' in comps:
                                trs = [tr for tr in trs if tr.channel in ['HHZ', 'HH1', 'HH2']]

                            elif 'EHZ' in comps and 'EHN' in comps and 'EHE' in comps:
                                trs = [tr for tr in trs if tr.channel in ['EHZ', 'EHN', 'EHE']]
                            elif 'EHZ' in comps and 'EH2' in comps and 'EH3' in comps:
                                trs = [tr for tr in trs if tr.channel in ['EHZ', 'EH2', 'EH3']]                            
                            elif 'EHZ' in comps and 'EH1' in comps and 'EH2' in comps:
                                trs = [tr for tr in trs if tr.channel in ['EHZ', 'EH1', 'EH2']] 

                            elif 'BHZ' in comps and 'BHN' in comps and 'BHE' in comps:
                                trs = [tr for tr in trs  if tr.channel in ['BHZ', 'BHN', 'BHE']] 
                            elif 'BHZ' in comps and 'BH2' in comps and 'BH3' in comps:
                                trs = [tr for tr in trs if tr.channel in ['BHZ', 'BH2', 'BH3']]
                            elif 'BHZ' in comps and 'BH1' in comps and 'BH2' in comps:
                                trs = [tr for tr in trs if tr.channel in ['BHZ', 'BH1', 'BH2']] 

                            elif 'LHZ' in comps and 'LHN' in comps and 'LHE' in comps:
                                trs = [tr for tr in trs if tr.channel in ['LHZ', 'LHN', 'LHE']]
                            elif 'LHZ' in comps and 'LH2' in comps and 'LH3' in comps:
                                trs = [tr for tr in trs if tr.channel in ['LHZ', 'LH2', 'LH3']]                            
                            elif 'LHZ' in comps and 'LH1' in comps and 'LH2' in comps:
                                trs = [tr for tr in trs if tr.channel in ['LHZ', 'LH1', 'LH2']] 

                            else:
                                # print('no BH* or HH* data for station %s found' % (str(nsl)))
                                # print('found these:', [tr.channel for tr in trs])
                                continue

                            for tr in trs:
                                cnt_resp = 0
                                for resp_now in responses:
                                    try:
                                        polezero_resp = resp_now.get_pyrocko_response(
                                            nslc=tr.nslc_id,
                                            timespan=(tr.tmin, tr.tmax),
                                            fake_input_units='M'
                                            )

                                        restituted = tr.transfer(
                                            tfade=transf_taper,
                                            freqlimits=RestDownconf.freqlim,
                                            transfer_function=polezero_resp,
                                            invert=True)

                                        rest_fn = dir_make + '/' + str(tr.nslc_id[0]) + '_' +\
                                                  str(tr.nslc_id[1])\
                                                  + '_' + str(tr.nslc_id[3]) + '_' +\
                                                  ev_t_str + 'rest2.mseed'
                                        io.save(restituted, rest_fn)

                                    except stationxml.NoResponseInformation:
                                        cnt_resp += 1
                                        if cnt_resp == i_resp:
                                            print('no resp found:', tr.nslc_id)

                                    except trace.TraceTooShort:
                                        print('trace too short', tr.nslc_id)

                                    except ValueError:
                                        print('downsampling does not work', tr.nslc_id)

                                    else:
                                        break


        ''' 5. Rotation NE --> RT '''
        if RestDownconf.rotate_data is True:
            print('Starting downsampling and rotation')

            def save_rot_down_tr(tr, dir_rot, ev_t_str):
                rot_fn = dir_rot + '/' + str(tr.nslc_id[0]) + '_' +\
                         str(tr.nslc_id[1])\
                         + '_' + str(tr.nslc_id[3]) + '_' +\
                         ev_t_str + 'rrd2.mseed'
                io.save(tr, rot_fn)


            def downsample_rotate(dir_rest, dir_rot, all_stations, st_xml, deltat_down):

                ev_data_pile = pile.make_pile(dir_rest, regex='rest2')
                for st in all_stations:
                    #if st.station not in st_liste_check:
                    #    continue
                    #if st.network not in net_check:
                    #    continue 
                    nsl = st.nsl()
                    trs = ev_data_pile.all(
                            trace_selector=lambda tr: tr.nslc_id[:2] == nsl[:2],
                            want_incomplete=True)

                    chan_avail = [tr.channel for tr in trs]
                    lens_trs = [len(tr.ydata) for tr in trs]

                    if not lens_trs or 0 in lens_trs or len(chan_avail) != 3:
                        continue

                    else:
                        az1 = None
                        tr1 = None
                        tr2 = None

                        nslcs = [tr.nslc_id for tr in trs]
                        tmin = max([tr.tmin for tr in trs])
                        tmax = min([tr.tmax for tr in trs])
                        trmin = math.ceil(tmin)
                        trmax = int(tmax)

                        for st_now in st_xml:
                            stat = st_now.get_pyrocko_stations(nslcs=nslcs,
                                   timespan=(trmin, trmax))
                            if len(stat) == 1:
                                break

                        if len(stat) != 0:
                            stat = stat[0]
                            tr1_ch = '0'
                            tr2_ch = '0'

                            test = []
                            naming = ''
                            for tr in trs:
                                if tr.channel.endswith('2'):
                                    test.append('2')
                                elif tr.channel.endswith('3'):
                                    test.append('3')
                                elif tr.channel.endswith('1'):
                                    test.append('1')
                            
                            if '1' in test and '2' in test:
                                naming = '1,2'
                                print('found 1,2')
                            elif  '2' in test and '3' in test:
                                naming = '2,3'

                            for tr in trs:
                                if tr.channel.endswith('N') or\
                                 (tr.channel.endswith('2') and naming == '2,3')\
                                  or (tr.channel.endswith('1') and naming == '1,2'):

                                    for ch in stat.channels:
                                        if tr.channel == ch.name:
                                            az1 = ch.azimuth
                                            tr1 = tr.copy()

                                            try:
                                                tr1.chop(trmin, trmax)
                                                if deltat_down > 0.0:
                                                    tr1.downsample_to(deltat=deltat_down)
                                                save_rot_down_tr(tr1, dir_rot, ev_t_str)

                                            except trace.NoData:
                                                print('N/2 comp no data in twd', nsl)
                                                tr1 = None
                                            except ValueError:
                                                tr1 = None
                                                print('N/2 downsampling not successfull')

                                if tr.channel.endswith('E') or\
                                 (tr.channel.endswith('3') and naming == '2,3')\
                                  or (tr.channel.endswith('2') and naming == '1,2'):

                                    for ch in stat.channels:
                                        if tr.channel == ch.name:
                                            tr2 = tr.copy()

                                            try:
                                                tr2.chop(trmin, trmax)
                                                if deltat_down > 0.0:                                                
                                                    tr2.downsample_to(deltat=deltat_down)
                                                save_rot_down_tr(tr2, dir_rot, ev_t_str)

                                            except trace.NoData:
                                                print('E/3 comp no data in twd', nsl)
                                                tr2 = None
                                            except ValueError:
                                                tr2 = None
                                                print('E/3 downsampling not successfull')

                                if tr.channel.endswith('Z'):
                                    trZ = tr.copy()

                                    try:
                                        trZ.chop(trmin, trmax)
                                        if deltat_down > 0.0:
                                            trZ.downsample_to(deltat=deltat_down)
                                        trZ.set_channel('Z')
                                        save_rot_down_tr(trZ, dir_rot, ev_t_str)

                                    except trace.NoData:
                                        print('E/3 comp no data in twd', nsl)
                                    except ValueError:
                                        trZ = None
                                        print('Z downsampling not successfull')
                                

                        if az1 is not None and tr1 is not None\
                          and tr2 is not None:

                            stat.set_event_relative_data(ev)
                            baz = stat.backazimuth
                            az_r = baz + 180 - az1
                           
                            if str(tr1.channel).endswith('N') is True\
                               or str(tr1.channel).endswith('2') is True and naming == '2,3'\
                               or str(tr1.channel).endswith('1') is True and naming == '1,2':
                                tr1_ch = tr1.channel

                            if str(tr1.channel).endswith('E') is True\
                              or str(tr1.channel).endswith('3') is True and naming == '2,3'\
                              or str(tr1.channel).endswith('2') is True and naming == '1,2':
                                tr2_ch = tr1.channel

                            if str(tr2.channel).endswith('N') is True\
                             or str(tr2.channel).endswith('2') is True and naming == '2,3'\
                             or str(tr2.channel).endswith('1') is True and naming == '1,2':
                                tr1_ch = tr2.channel

                            if str(tr2.channel).endswith('E') is True\
                             or str(tr2.channel).endswith('3') is True and naming == '2,3'\
                             or str(tr2.channel).endswith('2') is True and naming == '1,2':
                                tr2_ch = tr2.channel


                            if tr1_ch != '0' and tr2_ch != '0':
                                rots = trace.rotate(traces=[tr1,tr2], azimuth=az_r, 
                                                    in_channels=[tr1_ch, tr2_ch],
                                                    out_channels=['R', 'T'])
                                for tr in rots:
                                    save_rot_down_tr(tr, dir_rot, ev_t_str)


            st_xml = []
            if metaDataconf.local_metadata != []:
                for file in metaDataconf.local_metadata:
                    st_xml.append(stationxml.load_xml(filename=file))
            
            if metaDataconf.use_downmeta is True:            
                for site in sites:
                    stations_fn = data_dir + 'Resp_all_' + str(site) + '.xml'
                    st_xml.append(stationxml.load_xml(filename=stations_fn))

            i_st_xml = len(st_xml)
            for key, subset_catalog in subsets_events.items(): 

                for ev in subset_catalog:
                    gc.collect()
                    ev_t_str = util.time_to_str(ev.time).replace(' ', '_')
                    # print(ev_t_str)
                    os.makedirs(data_dir+'rrd/', exist_ok=True)
                    dir_rot = data_dir + 'rrd/' + ev_t_str
                    dir_rest = data_dir + 'rest/' + ev_t_str
                    downsample_rotate(dir_rest, dir_rot, all_stations, st_xml, RestDownconf.deltat_down)
                    print('saved ev ', util.time_to_str(ev.time))


        ''' 6. Synthetic data '''
        if synthsconf.make_syn_data is True:
            print('Starting to generate synthetic data')

            freqlim = RestDownconf.freqlim
            transf_taper = 1/min(freqlim)
            store_id = synthsconf.store_id
            engine = gf.LocalEngine(store_superdirs=
                                   [synthsconf.engine_path])
            os.makedirs(data_dir+'synthetics/', exist_ok=True)
            loc = '0'
            for key, subset_catalog in subsets_events.items(): 

                for ev in subset_catalog:

                    ev_t_str = util.time_to_str(ev.time).replace(' ', '_')
                    print(ev_t_str)

                    dir_syn_ev = data_dir + 'synthetics/' + ev_t_str
                    os.makedirs(dir_syn_ev, exist_ok=True)
                    # ev.duration = ev.
                    # ev.duration
                    # oder
                    # source.stf = gf.BoxcarSTF(duration=)
                    # scaling mit magnitude
                    if ev.duration < 1:
                        print('warning ev.duratiom')
                    #ev.duration = None
                    ev.time = ev.time + ev.duration/2
                    source = gf.MTSource.from_pyrocko_event(ev)

                    for st in all_stations:
                        #if st.station not in st_liste_check:
                        #    continue                        
                        targets = []
                        sta = st.station
                        net = st.network

                        for cha in ['Z', 'R', 'T']:
                            target = gf.Target(
                                    codes=(net, sta, loc, cha),
                                    quantity='displacement',
                                    lat=st.lat,
                                    lon=st.lon,
                                    store_id=store_id)
                            azi, _ = target.azibazi_to(ev)

                            if cha == 'R':
                                target.azimuth = azi - 180.
                                target.dip = 0.

                            elif cha == 'T':
                                target.azimuth = azi - 90.
                                target.dip = 0.

                            if cha == 'Z':
                                target.azimuth = 0.
                                target.dip = -90.

                            targets.append(target)

                            try:
                                response = engine.process(source, targets)
                                trs_syn = response.pyrocko_traces()

                            except:
                                continue

                            else:

                                for tr in trs_syn:
                                    # adding zeros at beginning of trace for taper in
                                    # tr.transfer
                                    tr.extend(tmin=tr.tmin-transf_taper)
                                    tr.transfer(
                                                tfade=transf_taper,
                                                freqlimits=freqlim,
                                                invert=False)
                                    net, sta, loc, cha = tr.nslc_id

                                    filename = '%s/syn_%s_%s_%s_%s.mseed'\
                                             % (dir_syn_ev, net, sta, cha, ev_t_str)
                                    io.save(tr, filename, format='mseed')


        ''' 7. Gain factors '''
        if gainfconf.calc_gainfactors is True:
            print('Starting evaluation of gainfactors.')
            datapath = data_dir + 'rrd/'
            os.makedirs(data_dir+'results/gains/', exist_ok=True)
            dir_gains = data_dir + 'results/gains/'
            twd = (gainfconf.wdw_st_arr, gainfconf.wdw_sp_arr)

            # arrival times
            if arrT_array is None:
                try:
                    data_dir = gensettings.data_dir
                    arrT_array = num.load(data_dir+'ttt/ArrivalTimes_deep.npy')
                except:
                    print('Please calculate arrival times first!')
                    sys.exit()


            def run_autogain(datapath, all_stations, subset_catalog,
                             gain_factor_method, dir_gains, 
                             twd, arrT_array, comp):
                if len(gain_factor_method) == 1:
                    gain_factor_method = gain_factor_method[0]
                data_pile = pile.make_pile(datapath)

                fband = gainfconf.fband
                taper = trace.CosFader(xfrac=gainfconf.taper_xfrac)

                ag = gainf.AutoGain(data_pile, stations=all_stations,
                                          events=subset_catalog,
                                          arrT=arrT_array,
                                          component=comp,
                                          gain_rel_to=gain_factor_method)

  
                ag.process(fband, taper, twd)

                # Store mean results in YAML format:
                print('saving mean gains: gains_median_and_mean%s.txt' % c, dir_gains)
                ag.save_median_and_mean_and_stdev('gains_median_and_mean%s.txt' % c, directory=dir_gains)
                # Store all results in comma-spread text file:
                ag.save_single_events('gains_all_events%s.txt' % c,
                                      directory=dir_gains, plot=gainfconf.plot_allgains)
                ag = None

            
            for c in gainfconf.components:
                print(c)
                run_autogain(datapath, all_stations, subsets_events['deep'],
                             gainfconf.gain_factor_method,
                             dir_gains, twd, arrT_array, c)
                  
            gc.collect()

        if gainfconf.plot_median_gain_on_map is True:
            dir_gains = data_dir + 'results/gains/'
            for c in gainfconf.components:
                plot_median_gain_map_from_file(ns, st_lats, st_lons, maps.pl_opt, maps.pl_topo,
                                             'gains_median_and_mean%s.txt' % c, dir_gains, c,
                                             maps.map_size)




        ''' 8. Frequency spectra'''
        # psd plot for each station, syn + obs
        # plot ratio of syn and obs for each station, single events
        # plot mean of psd ratio for each station
        # plot flat ranges of psd ratio
        # output flat-ratio-ranges as yaml file


        if psdsconf.calc_psd is True:
            print('starting calc_psd')
            dir_f = data_dir + '/results/freq/'
            os.makedirs(dir_f, exist_ok=True)

            datapath = data_dir + 'rrd/'
            syndatapath = data_dir + 'synthetics/'

            print(datapath, syndatapath)

            if arrT_array is None:
                try:
                    data_dir = gensettings.data_dir
                    arrT_array = num.load(data_dir+'ttt/ArrivalTimes_deep.npy')
                except:
                    print('Please calculate arrival times first!')
                    sys.exit()
            
            if arrT_R_array is None:
                try:
                    data_dir = gensettings.data_dir
                    arrT_R_array = num.load(data_dir+'ttt/ArrivalTimes_estR_deep.npy')
                except:
                    print('Please calculate R arrival times first!')
                    sys.exit()

            st_numbers = [i_st for i_st in range(len(all_stations))]
            flat_f_ranges_ll = []
            freq_rat_list_y_ll = []
            flat_by_next_ll = []
            freq_neigh_list_y_ll = []

            nslc_list = []
            nslc_list2 = []
            freq_neigh_list_y_ll = []

            for i_st, st in zip(st_numbers[0:10], all_stations[0:10]):
                print(i_st, st.station)
            #for i_st, st in enumerate(all_stations):

                freq_rat_list_st, freq_rat_list_y, nslc_list_st = fp.prep_psd_fct(
                  i_st, st, subsets_events['deep'],
                  dir_f,
                  arrT_array, arrT_R_array,
                  datapath, syndatapath,
                  psdsconf.tinc, psdsconf.tpad,
                  psdsconf.dt_start, psdsconf.dt_end,
                  psdsconf.n_poly,
                  psdsconf.norm_factor,
                  psdsconf.f_ign,
                  plot_psds=psdsconf.plot_psds,
                  plot_ratio_extra=psdsconf.plot_ratio_extra,
                  plot_m_rat=psdsconf.plot_m_rat,
                  plot_flat_ranges=psdsconf.plot_flat_ranges,
                  plot_neighb_ranges=psdsconf.plt_neigh_ranges)

                if freq_rat_list_st != [] and nslc_list_st != []:
                    flat_f_ranges_ll.extend(freq_rat_list_st)
                    freq_rat_list_y_ll.extend(freq_rat_list_y)
                    nslc_list.extend(nslc_list_st)
                '''
                if flat_by_next != [] and nslc_list_st != []:
                    flat_by_next_ll.extend(flat_by_next)
                    freq_neigh_list_y_ll.extend(flat_by_next_y)
                    nslc_list2.extend(nslc_list_st)
                '''

            fp.dump_flat_ranges(flat_f_ranges_ll, freq_rat_list_y_ll,
                                nslc_list, dir_f,
                                fname_ext='linefit2', only_first=psdsconf.only_first)
            '''
            dump_flat_ranges(flat_by_next_ll, freq_neigh_list_y_ll,
                                nslc_list2, dir_f,
                                fname_ext='neighbour2')
            '''


        # 9. Rayleigh wave polarization analysis for orientation
        if orientconf.orient_rayl == True:
            print('starting rayleigh wave orientation section')
            dir_ro = data_dir + 'results/orient/'
            os.makedirs(dir_ro, exist_ok=True)
            datapath = data_dir + 'rrd/'
            list_median_a = []
            list_mean_a = []
            list_stdd_a = []            
            list_switched = []
            used_stats = []
            list_all_angles = []
            n_ev = []

            st_numbers = [i_st for i_st in range(len(all_stations))]
            for i_st, st in zip(st_numbers, all_stations):
                print(st.station)
                out = orient.prep_orient(
                                        datapath,
                                        st,
                                        subsets_events['shallow'], 
                                        dir_ro,
                                        orientconf.bandpass,
                                        orientconf.start_before_ev,
                                        orientconf.stop_after_ev,
                                        orientconf.ccmin,
                                        orientconf.plot_heatmap,
                                        orientconf.plot_distr)

                if out:
                    list_median_a.append(out[0])
                    list_mean_a.append(out[1])
                    list_stdd_a.append(out[2])
                    list_switched.append(out[3])
                    list_all_angles.append(out[4])
                    n_ev.append(out[5])

                    used_stats.append(st)

            orient.write_output(list_median_a, list_mean_a, list_stdd_a, list_switched,
                                n_ev, used_stats, dir_ro, orientconf.ccmin)
            orient.write_all_output_csv(list_all_angles, used_stats, dir_ro)

        if orientconf.plot_orient_map_fromfile == True:
            dir_ro = data_dir + 'results/orient/'          
            orient.plot_corr_angles(ns, st_lats, st_lons,
                                    'CorrectionAngles.yaml', dir_ro,
                                    maps.pl_opt, maps.pl_topo,
                                    maps.map_size,
                                    orientconf.orient_map_label)
        if orientconf.plot_angles_vs_events == True:
            dir_ro = data_dir + 'results/orient/'
            orient.plot_corr_time(ns, 'AllCorrectionAngles.yaml', dir_ro)

if __name__ == '__main__':
    main()

