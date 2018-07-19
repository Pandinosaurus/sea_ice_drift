# Name:    pmlib.py
# Purpose: Container of Pattern Matching functions
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
from __future__ import absolute_import

from multiprocessing import Pool

import numpy as np
from scipy import ndimage as nd

import cv2
import gdal

from sea_ice_drift.lib import (x2y2_interpolation_poly,
                               x2y2_interpolation_near,
                               get_drift_vectors,
                               _fill_gpi)

shared_args = None
shared_kwargs = None

def get_hessian(ccm, hes_norm=True, hes_smth=False, **kwargs):
    """ Find Hessian of the input cross correlation matrix <ccm>

    Parameters
    ----------
    ccm : 2D numpy array, cross-correlation matrix
    hes_norm : bool, normalize Hessian by AVG and STD?
    hes_smth : bool, smooth Hessian?

    """
    if hes_smth:
        ccm2 = nd.filters.gaussian_filter(ccm, 1)
    else:
        ccm2 = ccm
    # Jacobian components
    dcc_dy, dcc_dx = np.gradient(ccm2)
    # Hessian components
    d2cc_dx2 = np.gradient(dcc_dx)[1]
    d2cc_dy2 = np.gradient(dcc_dy)[0]
    hes = np.hypot(d2cc_dx2, d2cc_dy2)
    if hes_norm:
        hes = (hes - np.median(hes)) / np.std(hes)

    return hes


def get_rotated_template(img, r, c, size, angle, rot_order=1, **kwargs):
    ''' Get rotated template of a given size
    Parameters
    ----------
    img : 2D numpy array - original image
    r : int - row coordinate of center
    c : int - column coordinate of center
    size : int - template size
    angle : float - rotation angle
    order : resampling order
    Returns
    -------
        templateRot : 2D numpy array - rotated subimage
    '''
    hws = size / 2.
    angle_rad = np.radians(angle)
    hwsrot = np.ceil(hws * np.abs(np.cos(angle_rad)) + hws * np.abs(np.sin(angle_rad)))
    hwsrot2 = np.ceil(hwsrot * np.abs(np.cos(angle_rad)) + hwsrot * np.abs(np.sin(angle_rad)))
    rotBorder1 = int(hwsrot2 - hws)
    rotBorder2 = int(rotBorder1 + hws + hws)

    # read large subimage
    if isinstance(img, np.ndarray):
        template = img[int(r-hwsrot):int(r+hwsrot+1), int(c-hwsrot):int(c+hwsrot+1)]
    elif isinstance(img, gdal.Dataset):
        template = img.ReadAsArray(xoff=int(c[0]-hwsrot),
                                   yoff=int(r[0]-hwsrot),
                                   xsize=int(hwsrot*2+1),
                                   ysize=int(hwsrot*2+1))

    templateRot = nd.interpolation.rotate(template, angle, order=rot_order)
    templateRot = templateRot[rotBorder1:rotBorder2, rotBorder1:rotBorder2]

    return templateRot

def get_distance_to_nearest_keypoint(x1, y1, shape):
    ''' Return full-res matrix with distance to nearest keypoint in pixels
    Parameters
    ----------
        x1 : 1D vector - X coordinates of keypoints
        y1 : 1D vector - Y coordinates of keypoints
        shape : shape of image
    Returns
    -------
        dist : 2D numpy array - image with distances
    '''
    seed = np.zeros(shape, dtype=bool)
    seed[np.uint16(y1), np.uint16(x1)] = True
    dist = nd.distance_transform_edt(~seed,
                                    return_distances=True,
                                    return_indices=False)
    return dist

def get_initial_rotation(n1, n2):
    ''' Returns angle <alpha> of rotation between two Nansat <n1>, <n2>'''
    corners_n2_lons, corners_n2_lats = n2.get_corners()
    corner0_n2_x1, corner0_n2_y1 = n1.transform_points([corners_n2_lons[0]], [corners_n2_lats[0]], 1)
    corner1_n2_x1, corner1_n2_y1 = n1.transform_points([corners_n2_lons[1]], [corners_n2_lats[1]], 1)
    b = corner1_n2_x1 - corner0_n2_x1
    a = corner1_n2_y1 - corner0_n2_y1
    alpha = np.degrees(np.arctan2(b, a)[0])
    return alpha

def rotate_and_match(img1, x, y, img_size, image, alpha0,
                     angles=[-3,0,3],
                     mtype=cv2.TM_CCOEFF_NORMED,
                     template_matcher=cv2.matchTemplate,
                     mcc_norm=False,
                     **kwargs):
    ''' Rotate template in a range of angles and run MCC for each
    Parameters
    ----------
        im1g : 2D numpy array - original image 1
        x : int - X coordinate of center
        y : int - Y coordinate of center
        img_size : size of template
        image : original image 2
        alpha0 : float - angle of rotation between two SAR scenes
        angles : list - which angles to test
        mtype : int - type of cross-correlation
        template_matcher : func - function to use for template matching
        mcc_norm : bool, normalize MCC by AVG and STD ?
        kwargs : dict, params for get_hessian
    Returns
    -------
        dx : int - X displacement of MCC
        dy : int - Y displacement of MCC
        best_a : float - angle of MCC
        best_r : float - MCC
        best_h : float - Hessian at highest MCC point
        best_result : 2D array - CC
        best_template : 2D array - template rotated to the best angle
    '''
    best_r = -np.inf
    for angle in angles:
        template = get_rotated_template(img1, y, x, img_size, angle-alpha0, **kwargs)
        if template.shape[0] < img_size or template.shape[1] < img_size:
            return np.nan, np.nan, np.nan, np.nan, np.nan, np.nan

        result = template_matcher(image, template.astype(np.uint8), mtype)
        ij = np.unravel_index(np.argmax(result), result.shape)

        if result.max() > best_r:
            best_r = result.max()
            best_a = angle
            best_result = result
            best_template = template
            best_ij = ij

    best_h = get_hessian(best_result, **kwargs)[best_ij]
    dy = best_ij[0] - (image.shape[0] - template.shape[0]) / 2.
    dx = best_ij[1] - (image.shape[1] - template.shape[1]) / 2.

    if mcc_norm:
        best_r = (best_r - np.median(best_result)) / np.std(best_result)

    return dx, dy, best_a, best_r, best_h, best_result, best_template

def use_mcc(x1p, y1p, x2p, y2p, border, img1, img2, img_size, alpha0, **kwargs):
    """ Apply MCC algorithm for one point

    Parameters
    ----------
        x1p : float, X coordinate on image 1
        y1p : float, Y coordinate on image 1
        x2p : float, first guess X coordinate on image 2
        y2p : float, first guess Y coordinate on image 2
        border : int, searching distance (border around template)
        img1 : 2D array - full szie image 1
        img2 : 2D array - full szie image 2
        img_size : int, template size
        alpha0 : float, rotation between two images
        kwargs : dict, params for rotate_and_match, get_rotated_template, get_hessian
    Returns
    -------
        x2 : float, result X coordinate on image 2
        y2 : float, result X coordinate on image 2
        a : float, angle that gives highest MCC
        r : float, MCC
        h : float, Hessian of CC at MCC point

    """
    x2p, y2p = int(round(x2p)), int(round(y2p))

    hws = int(img_size / 2.)
    image = img2[int(y2p-hws-border):int(y2p+hws+border+1),
                 int(x2p-hws-border):int(x2p+hws+border+1)]

    dx, dy, a, r, h, bestr, bestt = rotate_and_match(img1, x1p, y1p,
                                                     img_size, image,
                                                     alpha0, **kwargs)
    x2 = x2p + dx
    y2 = y2p + dy

    return x2, y2, a, r, h

def use_mcc_mp(i):
    """ Use MCC in multiprocessing
    Uses global variables where first guess and images are stored
    Parameters
    ---------
        i : int, index of point
    Returns
    -------
        x2 : float, result X coordinate on image 2
        y2 : float, result X coordinate on image 2
        a : float, angle that gives highest MCC
        r : float, MCC
        h : float, Hessian of CC at MCC point

    """
    global shared_args, shared_kwargs

    # structure of shared_args:
    # x1_dst, y1_dst, x2fg, y2fg, border, img1, img2, img_size, alpha0
    x2, y2, a, r, h = use_mcc(shared_args[0][i],
                              shared_args[1][i],
                              shared_args[2][i],
                              shared_args[3][i],
                              shared_args[4][i],
                              shared_args[5],
                              shared_args[6],
                              shared_args[7],
                              shared_args[8],
                              **shared_kwargs)
    if i % 100 == 0:
        print('%02.0f%% %07.1f %07.1f %07.1f %07.1f %+05.1f %04.2f %04.2f' % (
            100 * float(i) / len(shared_args[0]),
            shared_args[0][i], shared_args[1][i], x2, y2, a, r, h))
    return x2, y2, a, r, h

def prepare_first_guess(x1_dst, y1_dst, n1, x1, y1, n2, x2, y2, img_size,
                        min_fg_pts=5,
                        min_border=20,
                        max_border=50,
                        old_border=True, **kwargs):
    ''' For the given coordinates estimate the First Guess
    Parameters
    ---------
        x1_dst : 1D vector, X coordinates of results on image 1
        y1_dst : 1D vector, Y coordinates of results on image 1
        x1 : 1D vector, X coordinates of keypoints on image 1
        y1 : 1D vector, Y coordinates of keypoints on image 1
        x2 : 1D vector, X coordinates of keypoints on image 2
        y2 : 1D vector, Y coordinates of keypoints on image 2
        img1 : 2D array, the fist image
        img_size : int, size of template
        min_fg_pts : int, minimum number of fist guess points
        min_border : int, minimum searching distance
        max_border : int, maximum searching distance
        old_border : bool, use old border selection algorithm?
        **kwargs : parameters for:
            x2y2_interpolation_poly
            x2y2_interpolation_near
    Returns
    -------
        x2fg : 1D vector, first guess X coordinates of results on image 2
        y2fg : 1D vector, first guess X coordinates of results on image 2
        border : 1D vector, searching distance
    '''
    shape1 = n1.shape()
    if len(x1) > min_fg_pts:
        # interpolate 1st guess using 2nd order polynomial
        x2p2, y2p2 = x2y2_interpolation_poly(x1, y1, x2, y2,
                                             x1_dst, y1_dst, **kwargs)

        # interpolate 1st guess using griddata
        x2fg, y2fg = x2y2_interpolation_near(x1, y1, x2, y2,
                                             x1_dst, y1_dst, **kwargs)

        # TODO:
        # Now border is proportional to the distance to the point
        # BUT it assumes that:
        #     close to any point error is small, and
        #     error varies between points
        # What if error does not vary with distance from the point?
        # Border can be estimated as error of the first guess
        # (x2 - x2_predicted_with_polynom) gridded using nearest neighbour.
        if old_border:
            # find distance to nearest neigbour and create border matrix
            border_img = get_distance_to_nearest_keypoint(x1, y1, shape1)
            border = np.zeros(x1_dst.size) + max_border
            gpi = ((x1_dst >= 0) * (x1_dst < shape1[1]) *
                   (y1_dst >= 0) * (y1_dst < shape1[0]))
            border[gpi] = border_img[y1_dst.astype(np.int16)[gpi],
                                     x1_dst.astype(np.int16)[gpi]]
        else:
            x2tst, y2tst = x2y2_interpolation_poly(x1, y1, x2, y2, x1, y1,
                                                                    **kwargs)
            x2dif, y2dif = x2y2_interpolation_near(x1, y1,
                                                   x2-x2tst, y2-y2tst,
                                                   x1_dst, y1_dst,
                                                   **kwargs)
            border = np.hypot(x2dif, y2dif)

        # define searching distance
        border[border < min_border] = min_border
        border[border > max_border] = max_border
        border[np.isnan(y2fg)] = max_border

        # define FG based on P2 and GD
        x2fg[np.isnan(x2fg)] = x2p2[np.isnan(x2fg)]
        y2fg[np.isnan(y2fg)] = y2p2[np.isnan(y2fg)]
    else:
        lon_dst, lat_dst = n1.transform_points(x1_dst, y1_dst)
        x2fg, y2fg = n2.transform_points(lon_dst, lat_dst, 1)
        border = np.zeros(len(x1_dst)) + max_border*2

    return x2fg, y2fg, border

def pattern_matching(lon1_dst, lat1_dst,
                     n1, x1, y1, n2, x2, y2,
                     margin=0,
                     img_size=35,
                     threads=5,
                     **kwargs):
    ''' Run Pattern Matching Algorithm on two images
    Parameters
    ---------
        lon_dst : 1D vector, longitude of results on image 1
        lon_dst : 1D vector, latitude of results on image 1
        n1 : Nansat, the fist image with 2D array
        x1 : 1D vector, X coordinates of keypoints on image 1
        y1 : 1D vector, Y coordinates of keypoints on image 1
        n2 : Nansat, the second image with 2D array
        x2 : 1D vector, X coordinates of keypoints on image 2
        y2 : 1D vector, Y coordinates of keypoints on image 2
        img_size : int, size of template
        threads : int, number of parallel threads
        **kwargs : optional parameters for:
            prepare_first_guess
                min_fg_pts : int, minimum number of fist guess points
                min_border : int, minimum searching distance
                max_border : int, maximum searching distance
                old_border : bool, use old border selection algorithm?
            rotate_and_match
                angles : list - which angles to test
                mtype : int - type of cross-correlation
                template_matcher : func - function to use for template matching
                mcc_norm : bool, normalize MCC by AVG and STD ?
            get_rotated_template
                rot_order : resampling order for rotation
            get_hessian
                hes_norm : bool, normalize Hessian by AVG and STD?
                hes_smth : bool, smooth Hessian?
            get_drift_vectors
                nsr: Nansat.NSR(), projection that defines the grid
    Returns
    -------
        u : 1D vector, eastward ice drift speed, m/s
        v : 1D vector, northward ice drift speed, m/s
        a : 1D vector, angle that gives the highes MCC
        r : 1D vector, MCC
        h : 1D vector, Hessian of CC at MCC point
        lon2_dst : 1D vector, longitude of results on image 2
        lat2_dst : 1D vector, latitude  of results on image 2
    '''
    img1, img2 = n1[1], n2[1]
    dst_shape = lon1_dst.shape
    # convert lon/lat to pixe/line of the first image
    x1_dst, y1_dst = n1.transform_points(lon1_dst.flatten(), lat1_dst.flatten(), 1)

    x2fg, y2fg, border = prepare_first_guess(x1_dst, y1_dst,
                                             n1, x1, y1,
                                             n2, x2, y2,
                                             img_size,
                                             **kwargs)

    # find good input points
    hws = img_size / 2
    hws_hypot = np.hypot(hws, hws)
    gpi = ((x2fg-border-hws-margin > 0) *
           (y2fg-border-hws-margin > 0) *
           (x2fg+border+hws+margin < n2.shape()[1]) *
           (y2fg+border+hws+margin < n2.shape()[0]) *
           (x1_dst-hws_hypot-margin > 0) *
           (y1_dst-hws_hypot-margin > 0) *
           (x1_dst+hws_hypot+margin < n1.shape()[1]) *
           (y1_dst+hws_hypot+margin < n1.shape()[0]))

    alpha0 = get_initial_rotation(n1, n2)

    def _init_pool(*args):
        """ Initialize shared data for multiprocessing """
        global shared_args, shared_kwargs
        shared_args = args[:9]
        shared_kwargs = args[9]

    if threads == 0:
        # run MCC without threads
        _init_pool(x1_dst[gpi], y1_dst[gpi], x2fg[gpi], y2fg[gpi], border[gpi],
                      img1, img2, img_size, alpha0, kwargs)
        results = [use_mcc_mp(i) for i in range(len(gpi[gpi]))]
    else:
        # run MCC in multiple threads
        p = Pool(threads, initializer=_init_pool,
                initargs=(x1_dst[gpi], y1_dst[gpi], x2fg[gpi], y2fg[gpi], border[gpi],
                          img1, img2, img_size, alpha0, kwargs))
        results = p.map(use_mcc_mp, range(len(gpi[gpi])))
        p.close()
        p.terminate()
        p.join()
        del p

    if len(results) == 0:
        lon2_dst = np.zeros(dst_shape) + np.nan
        lat2_dst = np.zeros(dst_shape) + np.nan
        u = np.zeros(dst_shape) + np.nan
        v = np.zeros(dst_shape) + np.nan
        a = np.zeros(dst_shape) + np.nan
        r = np.zeros(dst_shape) + np.nan
        h = np.zeros(dst_shape) + np.nan
    else:
        results = np.array(results)

        x2_dst = results[:,0]
        y2_dst = results[:,1]
        a = results[:,2]
        r = results[:,3]
        h = results[:,4]

        u, v, lon1, lat1, lon2, lat2 = get_drift_vectors(n1, x1_dst[gpi], y1_dst[gpi],
                                                         n2, x2_dst, y2_dst,
                                                         dst_shape=dst_shape, gpi=gpi,
                                                         **kwargs)
        lon2_dst = _fill_gpi(dst_shape, gpi, lon2)
        lat2_dst = _fill_gpi(dst_shape, gpi, lat2)
        u = _fill_gpi(dst_shape, gpi, u)
        v = _fill_gpi(dst_shape, gpi, v)
        a = _fill_gpi(dst_shape, gpi, a)
        r = _fill_gpi(dst_shape, gpi, r)
        h = _fill_gpi(dst_shape, gpi, h)

    return u, v, a, r, h, lon2_dst, lat2_dst
