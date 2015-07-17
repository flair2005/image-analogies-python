import os
import pickle
import time
import warnings

import matplotlib.pyplot as plt
import numpy as np

from img_preprocess import convert_to_YIQ, convert_to_RGB, compute_gaussian_pyramid, initialize_Bp, remap_luminance, \
                           ix2px, savefig_noborder, pad_img_pair
from algorithms import create_index, extract_pixel_feature, best_approximate_match, best_coherence_match, \
                              compute_distance


def img_setup(A_fname, Ap_fname, B_fname, out_path, c):
    if not os.path.exists(out_path):
        os.makedirs(out_path)

    A_orig  = plt.imread(A_fname)
    Ap_orig = plt.imread(Ap_fname)
    B_orig  = plt.imread(B_fname)

    assert(A_orig.shape == Ap_orig.shape)  # src alignment
    assert(len(A_orig.shape) == len(B_orig.shape))  # same number of channels (for now)

    # Do conversions
    if c.convert:
        A_yiq  = convert_to_YIQ( A_orig/255.)
        Ap_yiq = convert_to_YIQ(Ap_orig/255.)
        B_yiq  = convert_to_YIQ( B_orig/255.)
        A  =  A_yiq[:, :, 0]
        Ap = Ap_yiq[:, :, 0]
        B  =  B_yiq[:, :, 0]
    else:
        A  =  A_orig/255.
        Ap = Ap_orig/255.
        B  =  B_orig/255.

    # Remap Luminance
    if c.remap_lum:
        A, Ap = remap_luminance(A, Ap, B)

    c.num_ch, c.padding_sm, c.padding_lg, c.weights = c.setup_vars(A)

    # Create Pyramids
    A_pyr  = compute_gaussian_pyramid( A, c.n_sm)
    Ap_pyr = compute_gaussian_pyramid(Ap, c.n_sm)
    B_pyr  = compute_gaussian_pyramid( B, c.n_sm)

    if c.convert:
        color_pyr = compute_gaussian_pyramid(B_yiq, c.n_sm)
    else:
        color_pyr = compute_gaussian_pyramid(Ap_orig, c.n_sm)

    if len(A_pyr) != len(B_pyr):
        c.max_levels = min(len(A_pyr), len(B_pyr))
        warnings.warn('Warning: input images are very different sizes! The minimum number of levels will be used.')
    else:
        c.max_levels = len(B_pyr)

    # Create Random Initialization of Bp
    Bp_pyr = initialize_Bp(B_pyr, init_rand=c.init_rand)

    return A_pyr, Ap_pyr, B_pyr, Bp_pyr, color_pyr, c


def image_analogies_main(A_fname, Ap_fname, B_fname, out_path, c, debug=False):
    # # This is the setup code
    begin_time = time.time()
    start_time = time.time()

    A_pyr, Ap_pyr, B_pyr, Bp_pyr, color_pyr, c = img_setup(A_fname, Ap_fname, B_fname, out_path, c)
    with open(out_path + 'metadata.pickle', 'w') as f:
        pickle.dump([A_fname, Ap_fname, B_fname, out_path, c], f)

    stop_time = time.time()
    print 'Environment Setup: %f' % (stop_time - start_time)

    # Build Structures for ANN
    start_time = time.time()

    flann, flann_params, As, As_size = create_index(A_pyr, Ap_pyr, c)

    stop_time = time.time()
    ann_time_total = stop_time - start_time
    print 'ANN Index Creation: %f' % (ann_time_total)

    # # This is the Algorithm Code

    # now we iterate per pixel in each level
    for level in range(1, c.max_levels):
        start_time = time.time()
        ann_time_level = 0
        print('Computing level %d of %d' % (level, c.max_levels - 1))

        imh, imw = Bp_pyr[level].shape[:2]
        color_im_out = np.nan * np.ones((imh, imw, 3))

        # pad each pyramid level to avoid edge problems
        A_pd  = pad_img_pair( A_pyr[level - 1],  A_pyr[level], c)
        Ap_pd = pad_img_pair(Ap_pyr[level - 1], Ap_pyr[level], c)
        B_pd  = pad_img_pair( B_pyr[level - 1],  B_pyr[level], c)

        s = []

        if debug:
            # make debugging structures
            sa = []
            sc = []
            rstars = []
            p_src     = np.nan * np.ones((imh, imw, 3))
            app_dist  = np.zeros((imh, imw))
            coh_dist  = np.zeros((imh, imw))
            app_color = np.array([1, 0, 0])
            coh_color = np.array([1, 1, 0])
            err_color = np.array([0, 0, 0])

            paths = ['%d_psrc.eps'    % (level),
                     '%d_appdist.eps' % (level),
                     '%d_cohdist.eps' % (level),
                     '%d_output.eps'  % (level)]
            vars = [p_src, app_dist, coh_dist, Bp_pyr[level]]

        for row in range(imh):
            for col in range(imw):
                Bp_pd = pad_img_pair(Bp_pyr[level - 1], Bp_pyr[level], c)
                BBp_feat = np.hstack([extract_pixel_feature( B_pd, (row, col), c, full_feat=True),
                                      extract_pixel_feature(Bp_pd, (row, col), c, full_feat=False)])
                assert(BBp_feat.shape == (As_size[level][1],))

                # Find Approx Nearest Neighbor
                ann_start_time = time.time()

                p_app_ix = best_approximate_match(flann[level], flann_params[level], BBp_feat)
                assert(p_app_ix < As_size[level][0])

                ann_stop_time = time.time()
                ann_time_level = ann_time_level + ann_stop_time - ann_start_time

                # translate p_app_ix back to row, col
                Ap_imh, Ap_imw = Ap_pyr[level].shape[:2]
                p_app = ix2px(p_app_ix, Ap_imw)

                # is this the first iteration for this level?
                # then skip coherence step
                if len(s) < 1:
                    p = p_app

                # Find Coherence Match and Compare Distances
                else:
                    p_coh, r_star = best_coherence_match(As[level], A_pyr[level].shape, BBp_feat, s, (row, col, imw), c)

                    if p_coh == (-1, -1):
                        p = p_app

                    else:
                        AAp_feat_app = np.hstack([extract_pixel_feature( A_pd, p_app, c, full_feat=True),
                                                  extract_pixel_feature(Ap_pd, p_app, c, full_feat=False)])

                        AAp_feat_coh = np.hstack([extract_pixel_feature( A_pd, p_coh, c, full_feat=True),
                                                  extract_pixel_feature(Ap_pd, p_coh, c, full_feat=False)])

                        d_app = compute_distance(AAp_feat_app, BBp_feat, c.weights)
                        d_coh = compute_distance(AAp_feat_coh, BBp_feat, c.weights)

                        if d_coh <= d_app * (1 + (2**(level - c.max_levels)) * c.k):
                            p = p_coh
                        else:
                            p = p_app

                # Update Bp and s
                Bp_pyr[level][row, col] = Ap_pyr[level][p]

                if not c.convert:
                    color_im_out[row, col, :] = color_pyr[level][p]

                s.append(p)

                if debug:
                    sa.append(p_app)
                    if len(s) > 1 and p_coh != (-1, -1):
                        sc.append(p_coh)
                        rstars.append(r_star)
                        app_dist[row, col] = d_app
                        coh_dist[row, col] = d_coh
                        if p == p_coh:
                            p_src[row, col] = coh_color
                        elif p == p_app:
                            p_src[row, col] = app_color
                        else:
                            print('There is a bug!')
                            raise
                    else:
                        sc.append((0, 0))
                        rstars.append((0, 0))
                        p_src[row, col] = err_color

        ann_time_total = ann_time_total + ann_time_level

        if debug:
            # Save debugging structures
            for path, var in zip(paths, vars):
                fig = plt.imshow(var, interpolation='nearest', cmap='gray')
                savefig_noborder(out_path + path, fig)
                plt.close()

            with open(out_path + '%d_srcs.pickle' % level, 'w') as f:
                pickle.dump([sa, sc, rstars, s], f)

        # Save color output images
        if c.convert:
            color_im_out = convert_to_RGB(np.dstack([Bp_pyr[level], color_pyr[level][:, :, 1:]]))
            color_im_out = np.clip(color_im_out, 0, 1)
        plt.imsave(out_path + 'level_%d_color.jpg' % level, color_im_out)

        stop_time = time.time()
        print 'Level %d time: %f' % (level, stop_time - start_time)
        print('Level %d ANN time: %f' % (level, ann_time_level))

    end_time = time.time()
    print 'Total time: %f' % (end_time - begin_time)
    print('ANN time: %f' % ann_time_total)