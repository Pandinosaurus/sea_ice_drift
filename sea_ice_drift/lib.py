# Name:    lib.py
# Purpose: Container of common functions
# Authors:      Anton Korosov, Stefan Muckenhuber
# Created:      21.09.2016
# Copyright:    (c) NERSC 2016
# Licence:
# This file is part of SeaIceDrift.
# SeaIceDrift is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3 of the License.
# http://www.gnu.org/licenses/gpl-3.0.html
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
from __future__ import absolute_import, print_function
import matplotlib.pyplot as plt

import numpy as np
from scipy.ndimage import zoom, maximum_filter
from scipy.interpolate import griddata
import gdal

from nansat import Nansat, Domain, NSR

AVG_EARTH_RADIUS = 6371  # in km

def get_uint8_image(image, vmin, vmax):
    ''' Scale image from float (or any) input array to uint8
    Parameters
    ----------
        image : 2D matrix
        vmin : float - minimum value
        vmax : float - maximum value
    Returns
    -------
        2D matrix
    '''
    if vmin is None or vmax is None:
        vmin, vmax = np.nanpercentile(image, [10, 99.9])
    # redistribute into range [1,255]
    # 0 is reserved for invalid pixels
    uint8Image = 1 + 254 * (image - vmin) / (vmax - vmin)
    uint8Image[uint8Image < 1] = 1
    uint8Image[uint8Image > 255] = 255
    uint8Image[~np.isfinite(image)] = 0

    return uint8Image.astype('uint8')

def get_displacement_km(n1, x1, y1, n2, x2, y2):
    ''' Find displacement in kilometers using Haversine
        http://www.movable-type.co.uk/scripts/latlong.html
    Parameters
    ----------
        n1 : First Nansat object
        x1 : 1D vector - X coordinates of keypoints on image 1
        y1 : 1D vector - Y coordinates of keypoints on image 1
        n2 : Second Nansat object
        x1 : 1D vector - X coordinates of keypoints on image 2
        y1 : 1D vector - Y coordinates of keypoints on image 2
    Returns
    -------
        h : 1D vector - total displacement, km
    '''
    lon1, lat1 = n1.transform_points(x1, y1)
    lon2, lat2 = n2.transform_points(x2, y2)

    lt1, ln1, lt2, ln2 = map(np.radians, (lat1, lon1, lat2, lon2))
    dlat = lt2 - lt1
    dlon = ln2 - ln1
    d = (np.sin(dlat * 0.5) ** 2 +
         np.cos(lt1) * np.cos(lt2) * np.sin(dlon * 0.5) ** 2)
    return 2 * AVG_EARTH_RADIUS * np.arcsin(np.sqrt(d))

def get_speed_ms(n1, x1, y1, n2, x2, y2):
    ''' Find ice drift speed in m/s
    Parameters
    ----------
        n1 : First Nansat object
        x1 : 1D vector - X coordinates of keypoints on image 1
        y1 : 1D vector - Y coordinates of keypoints on image 1
        n2 : Second Nansat object
        x1 : 1D vector - X coordinates of keypoints on image 2
        y1 : 1D vector - Y coordinates of keypoints on image 2
    Returns
    -------
        spd : 1D vector - speed, m/s
    '''
    dt = (n2.time_coverage_start - n1.time_coverage_start).total_seconds()
    return 1000.*get_displacement_km(n1, x1, y1, n2, x2, y2)/abs(dt)

def get_displacement_pix(n1, x1, y1, n2, x2, y2):
    ''' Find displacement in pixels of the first image
    Parameters
    ----------
        n1 : First Nansat object
        x1 : 1D vector - X coordinates of keypoints on image 1
        y1 : 1D vector - Y coordinates of keypoints on image 1
        n2 : Second Nansat object
        x1 : 1D vector - X coordinates of keypoints on image 2
        y1 : 1D vector - Y coordinates of keypoints on image 2
    Returns
    -------
        dx : 1D vector - leftward displacement, pix
        dy : 1D vector - upward displacement, pix
    '''
    lon2, lat2 = n2.transform_points(x2, y2)
    x2n1, y2n1 = n1.transform_points(lon2, lat2, 1)

    return x2n1 - x1, y2n1 - y1

def get_denoised_object(filename, bandName, factor, **kwargs):
    ''' Use sentinel1denoised and preform thermal noise removal
    Import is done within the function to make the dependency not so strict
    '''
    from sentinel1denoised.S1_EW_GRD_NoiseCorrection import Sentinel1Image
    s = Sentinel1Image(filename)
    s.add_denoised_band('sigma0_HV', **kwargs)
    s.resize(factor, eResampleAlg=-1)
    img = s[bandName + '_denoised']

    n = Nansat(domain=s)
    n.add_band(img, parameters=s.get_metadata(bandID=bandName))
    n.set_metadata(s.get_metadata())

    return n

def interpolation_poly(x1, y1, x2, y2, x1grd, y1grd, order=1, **kwargs):
    ''' Interpolate values of x2/y2 onto full-res grids of x1/y1 using
    polynomial of order 1 (or 2 or 3)
    Parameters
    ----------
        x1 : 1D vector - X coordinates of keypoints on image 1
        y1 : 1D vector - Y coordinates of keypoints on image 1
        x1 : 1D vector - X coordinates of keypoints on image 2
        y1 : 1D vector - Y coordinates of keypoints on image 2
        x1grd : 1D vector - source X coordinate on img1
        y1grd : 1D vector - source Y coordinate on img2
        order : [1,2,3] - order of polynom
    Returns
    -------
        x2grd : 1D vector - destination X coordinate on img1
        y2grd : 1D vector - destination Y coordinate on img2
    '''
    A = [np.ones(len(x1)), x1, y1]
    if order > 1:
        A += [x1**2, y1**2, x1*y1]
    if order > 2:
        A += [x1**3, y1**3, x1**2*y1, y1**2*x1]

    A = np.vstack(A).T
    Bx = np.linalg.lstsq(A, x2, rcond=-1)[0]
    By = np.linalg.lstsq(A, y2, rcond=-1)[0]
    x1grdF = x1grd.flatten()
    y1grdF = y1grd.flatten()

    A = [np.ones(len(x1grdF)), x1grdF, y1grdF]
    if order > 1:
        A += [x1grdF**2, y1grdF**2, x1grdF*y1grdF]
    if order > 2:
        A += [x1grdF**3, y1grdF**3, x1grdF**2*y1grdF, y1grdF**2*x1grdF]
    A = np.vstack(A).T
    x2grd = np.dot(A, Bx).reshape(x1grd.shape)
    y2grd = np.dot(A, By).reshape(x1grd.shape)

    return x2grd, y2grd

def interpolation_near(x1, y1, x2, y2, x1grd, y1grd, method='linear', **kwargs):
    ''' Interpolate values of x2/y2 onto full-res grids of x1/y1 using
    linear interpolation of nearest points
    Parameters
    ----------
        x1 : 1D vector - X coordinates of keypoints on image 1
        y1 : 1D vector - Y coordinates of keypoints on image 1
        x1 : 1D vector - X coordinates of keypoints on image 2
        y1 : 1D vector - Y coordinates of keypoints on image 2
        x1grd : 1D vector - source X coordinate on img1
        y1grd : 1D vector - source Y coordinate on img2
        method : str - parameter for SciPy griddata
    Returns
    -------
        x2grd : 1D vector - destination X coordinate on img1
        y2grd : 1D vector - destination Y coordinate on img2
    '''
    src = np.array([y1, x1]).T
    dst = np.array([y1grd, x1grd]).T
    x2grd = griddata(src, x2, dst, method=method).T
    y2grd = griddata(src, y2, dst, method=method).T

    return x2grd, y2grd

def get_n(filename, bandName='sigma0_HV',
                    factor=0.5,
                    vmin=-30,
                    vmax=-5,
                    denoise=False,
                    dB=True,
                    add_landmask=True,
                    **kwargs):
    """ Get Nansat object with image data scaled to UInt8
    Parameters
    ----------
    filename : str
        input file name
    bandName : str
        name of band in the file
    factor : float
        subsampling factor
    vmin : float
        minimum allowed value in the band
    vmax : float
        maximum allowed value in the band
    denoise : bool
        apply denoising of sigma0 ?
    dB : bool
        apply conversion to dB ?
    add_landmask : bool
        mask land with 0 ?
    **kwargs : parameters for get_denoised_object() and mask_land()

    Returns
    -------
        n : Nansat object with one band scaled to UInt8

    """
    if denoise:
        # run denoising
        n = get_denoised_object(filename, bandName, factor, **kwargs)
    else:
        # open data with Nansat and downsample
        n = Nansat(filename)
        if factor != 1:
            n.resize(factor, resample_alg=-1)
    # get matrix with data
    img = n[bandName]
    # convert to dB
    if not denoise and dB:
        img = 10 * np.log10(img)

    if add_landmask:
        img = mask_land(img, n, **kwargs)

    # convert to 1 - 255
    img = get_uint8_image(img, vmin, vmax)


    nout = Nansat.from_domain(n, img, parameters={'name': bandName})
    nout.set_metadata(n.get_metadata())
    # improve geolocation accuracy
    if len(nout.vrt.dataset.GetGCPs()) > 0:
        nout.reproject_gcps()
        nout.vrt.tps = True

    return nout

def mask_land(img, n, landmask_border=20, **kwargs):
    """
    Replace land and cosatal pixels input image with np.nan

    Parameters
    ----------
    img : float ndarray
        input image
    n : Nansat
        input Nansat object
    landmask_border : int
        border around landmask
    **kwargs : dict
        dummy params

    Returns
    -------
    img : float ndarray
        image with zero in land pixels
    """
    n.resize(1./landmask_border)
    try:
        wm = n.watermask()[1]
    except:
        print('Cannot add landmask')
        return img
    else:
        n.undo()
        wm[wm > 2] = 2
        wmf = maximum_filter(wm, 3)
        wmz = zoom(wmf, (np.array(n.shape()) / np.array(wm.shape)))
        img[wmz == 2] = np.nan
        img[np.isinf(img)] = np.nan
        return img

def get_drift_vectors(n1, x1, y1, n2, x2, y2, nsr=NSR(), **kwargs):
    ''' Find ice drift speed m/s
    Parameters
    ----------
        n1 : First Nansat object
        x1 : 1D vector - X coordinates of keypoints on image 1
        y1 : 1D vector - Y coordinates of keypoints on image 1
        n2 : Second Nansat object
        x1 : 1D vector - X coordinates of keypoints on image 2
        y1 : 1D vector - Y coordinates of keypoints on image 2
        nsr: Nansat.NSR(), projection that defines the grid
    Returns
    -------
        u : 1D vector - eastward ice drift speed
        v : 1D vector - northward ice drift speed
        lon1 : 1D vector - longitudes of source points
        lat1 : 1D vector - latitudes of source points
        lon2 : 1D vector - longitudes of destination points
        lat2 : 1D vector - latitudes of destination points
    '''
    # convert x,y to lon, lat
    lon1, lat1 = n1.transform_points(x1, y1)
    lon2, lat2 = n2.transform_points(x2, y2)

    # create domain that converts lon/lat to units of the projection
    d = Domain(nsr, '-te -10 -10 10 10 -tr 1 1')

    # find displacement in needed units
    x1, y1 = d.transform_points(lon1, lat1, 1)
    x2, y2 = d.transform_points(lon2, lat2, 1)

    return x2-x1, y1-y2, lon1, lat1, lon2, lat2

def _fill_gpi(shape, gpi, data):
    ''' Fill 1D <data> into 2D matrix with <shape> based on 1D <gpi> '''
    y = np.zeros(shape).flatten() + np.nan
    y[gpi] = data
    return y.reshape(shape)
