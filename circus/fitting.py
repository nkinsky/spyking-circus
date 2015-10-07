from .shared.utils import *

def main(filename, params, nb_cpu, use_gpu):

    #################################################################
    sampling_rate  = params.getint('data', 'sampling_rate')
    N_e            = params.getint('data', 'N_e')
    N_t            = params.getint('data', 'N_t')
    N_total        = params.getint('data', 'N_total')
    template_shift = params.getint('data', 'template_shift')
    file_out       = params.get('data', 'file_out')
    file_out_suff  = params.get('data', 'file_out_suff')
    spikedetekt    = params.getboolean('data', 'spikedetekt')
    do_temporal_whitening = params.getboolean('whitening', 'temporal')
    do_spatial_whitening  = params.getboolean('whitening', 'spatial')
    chunk_size     = int(params.getfloat('fitting', 'chunk')*sampling_rate)
    gpu_only       = params.getboolean('fitting', 'gpu_only')
    nodes, edges   = io.get_nodes_and_edges(params)
    tmp_limits     = params.get('fitting', 'amp_limits').replace('(', '').replace(')', '').split(',')
    tmp_limits     = map(float, tmp_limits)
    amp_auto       = params.getboolean('fitting', 'amp_auto')
    space_explo    = params.getfloat('fitting', 'space_explo')
    refractory     = float(params.getfloat('fitting', 'refractory')*sampling_rate*1e-3)
    nb_chances     = params.getint('fitting', 'nb_chances')
    max_chunk      = params.getfloat('fitting', 'max_chunk')
    spike_range    = int(params.getfloat('fitting', 'spike_range')*sampling_rate*1e-3)
    #################################################################

    if use_gpu:
        import cudamat as cmt
        if socket.gethostname() == 'spikesorter' and comm.rank > 4:
            cmt.cuda_set_device(1)
        else:
            cmt.cuda_set_device(0)
        cmt.init()
        cmt.cuda_sync_threads()

    templates      = io.load_data(params, 'templates')
    N_e, N_t, N_tm = templates.shape
    template_shift = int((N_t-1)/2)
    temp_2_shift   = 2*template_shift
    full_gpu       = use_gpu and gpu_only
    n_tm           = N_tm/2
    last_spikes    = numpy.zeros((n_tm, 1), dtype=numpy.int32)

    if not amp_auto:
        amp_limits       = numpy.zeros((n_tm, 2))
        amp_limits[:, 0] = tmp_limits[0]
        amp_limits[:, 1] = tmp_limits[1]
    else:
        amp_limits       = io.load_data(params, 'limits')

    #print "Normalizing the templates..."
    norm_templates = numpy.sqrt(numpy.mean(numpy.mean(templates**2,0), 0))
    templates     /= norm_templates
    info_string    = ''

    if comm.rank == 0:
        if use_gpu:
            if gpu_only:
                info_string = "using %d GPUs" %(comm.size)
            else:
                info_string = "using %d GPUs (projection only)" %(comm.size)
        else:
            info_string = "using %d CPUs" %(comm.size)


    if comm.rank == 0:
        print "Here comes the SpyKING CIRCUS %s..." %info_string
        io.purge(file_out_suff, '.data')

    comm.Barrier()

    thresholds    = io.load_data(params, 'thresholds')
    c_overlap     = io.get_overlaps(params)

    if do_spatial_whitening or do_temporal_whitening:
        spatial_whitening  = io.load_data(params, 'spatial_whitening')
        temporal_whitening = io.load_data(params, 'temporal_whitening')

    if spikedetekt:
        spiketimes = io.load_data(params, 'spikedetekt')

    if full_gpu:
        try:
            N_over    = c_overlap.shape[0]
            c_overs   = {}
            # If memory on the GPU is large enough, we load the overlaps onto it
            for i in xrange(N_over):
                c_overs[i] = cmt.CUDAMatrix(-c_overlap[i])
            del c_overlap
        except Exception:
            if comm.rank == 0:
                io.print_info(["Not enough memory on GPUs: GPUs are used for projection only"])
            for i in xrange(N_over):
                if c_overs.has_key(i):
                    del c_overs[i]
            full_gpu = False

    borders, nb_chunks, chunk_len, last_chunk_len = io.analyze_data(params, chunk_size)
    nb_chunks                                     = int(min(nb_chunks, max_chunk))

    if comm.rank == 0:
        pbar = progressbar.ProgressBar(widgets=[progressbar.Percentage(), progressbar.Bar(), progressbar.ETA()], maxval=int(nb_chunks/comm.size)).start()


    spiketimes_file = open(file_out_suff + '.spiketimes-%d.data' %comm.rank, 'w')
    amplitudes_file = open(file_out_suff + '.amplitudes-%d.data' %comm.rank, 'w')
    templates_file  = open(file_out_suff + '.templates-%d.data' %comm.rank, 'w')

    for gcount, gidx in enumerate(xrange(comm.rank, nb_chunks, comm.size)):
        #print "Node", comm.rank, "is analyzing chunk", gidx, "/", nb_chunks, " ..."
        ## We need to deal with the borders by taking chunks of size [0, chunck_size+template_shift]
        if gidx == (nb_chunks - 1):
            padding = (-2*borders, 0)
        elif gidx == 0:
            padding = (0, 2*borders)
        else:
            padding = (-2*borders, 2*borders)

        result       = {'spiketimes' : [], 'amplitudes' : [], 'templates' : []}

        local_chunk, local_shape = io.load_chunk(params, gidx, chunk_len, chunk_size, padding, nodes=nodes)
        if do_spatial_whitening:
            local_chunk = numpy.dot(local_chunk, spatial_whitening)
        if do_temporal_whitening:
            for i in xrange(N_e):
                local_chunk[:, i] = numpy.convolve(local_chunk[:, i], temporal_whitening, 'same')

        #print "Extracting the peaks..."
        if not spikedetekt:
            local_peaktimes = []
            for i in xrange(N_e):
                local_peaktimes += algo.detect_peaks(local_chunk[:, i], thresholds[i], valley=True).tolist()
        else:
            idx             = numpy.where((spiketimes >= gidx*chunk_size) & (spiketimes < (gidx+1)*chunk_size))[0]
            local_peaktimes = spiketimes[idx] - gidx*chunk_size

        spikes = numpy.unique(local_peaktimes)
        for spike in spikes:
            local_peaktimes += range(spike-spike_range, spike+spike_range)

        local_peaktimes = numpy.unique(numpy.array(local_peaktimes, dtype=numpy.int32))

        #print "Removing the useless borders..."
        local_borders   = (template_shift, local_shape - template_shift)
        idx             = (local_peaktimes >= local_borders[0]) & (local_peaktimes < local_borders[1])
        local_peaktimes = local_peaktimes[idx]
        n_t             = len(local_peaktimes)

        if n_t > 0:
            #print "Computing the b (should full_gpu by putting all chunks on GPU if possible?)..."
            if use_gpu:
                b    = cmt.CUDAMatrix(numpy.zeros((n_t, N_tm)))
                cloc = cmt.CUDAMatrix(local_chunk.T)
            else:
                b    = numpy.zeros((n_t, N_tm), dtype=numpy.float32)

            if use_gpu:
                sub_mat  = cmt.empty((N_e, n_t))

            try:
                for itime in xrange(temp_2_shift+1):
                    if use_gpu:
                        cu_slice = cmt.CUDAMatrix((local_peaktimes+itime-template_shift).reshape(1, n_t))
                        cloc.select_columns(cu_slice, sub_mat)
                        sub_mat_transpose = sub_mat.transpose()
                        sub_templates     = cmt.CUDAMatrix(templates[:, itime, :])
                        b.add_dot(sub_mat_transpose, sub_templates)
                        del sub_templates, sub_mat_transpose
                    else:
                        sub_mat = local_chunk[local_peaktimes+itime-template_shift, :]
                        b      += numpy.dot(sub_mat, templates[:, itime, :])
            except Exception:
                if comm.rank == 0:
                    lines = ["There may be a GPU memory error: -set gpu_only to False",
                             "                                 -reduce N_t",
                             "                                 -increase mergings"]
                    io.print_info(lines)

            if use_gpu:
                del sub_mat, cloc

            local_offset = gidx*chunk_size+padding[0]/N_total
            local_bounds = (temp_2_shift, local_shape - temp_2_shift)
            all_spikes   = local_peaktimes + local_offset

            # We penalize the neurons that are in refractory periods
            if refractory > 0:
                tmp     = numpy.dot(numpy.ones((n_tm, 1), dtype=numpy.int32), all_spikes.reshape((1, n_t)))
                penalty = 1 - numpy.exp((last_spikes - tmp)/refractory)
            else:
                penalty = numpy.ones((n_tm, n_t), dtype=numpy.float32)

            # Because for GPU, slicing by columns is more efficient, we need to transpose b
            b           = b.transpose()
            if use_gpu and not full_gpu:
                b = b.asarray()

            n_scalar    = N_e*N_t
            failure     = numpy.zeros(n_t, dtype=numpy.int32)

            if full_gpu:
                mask     = cmt.CUDAMatrix(penalty)
                data     = cmt.empty(mask.shape)
                cm_zeros = cmt.CUDAMatrix(numpy.zeros(mask.shape))
                patch_gpu= b.shape[1] == 1
            else:
                mask     = penalty
                sub_b    = b[:n_tm, :]


            if spike_range > 0:
                term_1 = numpy.dot(numpy.ones((n_tm, 1)), local_peaktimes[:-1].reshape(1, n_t-1))
                term_2 = numpy.dot(numpy.ones((n_tm, 1)), local_peaktimes[1:].reshape(1, n_t-1))-1
                term_3 = numpy.hstack((numpy.zeros((term_1.shape[0], 1)), term_1))[:, :-1]
                term_4 = numpy.hstack((numpy.zeros((term_2.shape[0], 1)), term_2))[:, :-1]

                if full_gpu:
                    c   = b.asarray()
                    idx = numpy.where(numpy.logical_or(c[:n_tm,:-1] > c[:n_tm,1:] * (term_3 == term_4), (term_1 != term_2)))
                    sub_mask  = numpy.ones((penalty.shape), dtype=numpy.float32)
                    sub_mask[idx[0], idx[1]+1] = 0
                else:
                    idx = numpy.where(numpy.logical_or(b[:n_tm,:-1] > b[:n_tm,1:] * (term_3 == term_4), (term_1 != term_2)))
                    mask[idx[0], idx[1]+1] = 0

                if full_gpu:
                    idx       = numpy.where(numpy.logical_or(c[:n_tm,:-1] < c[:n_tm,1:] * (term_1 == term_2), (term_1 != term_2)))
                    sub_mask[idx] = 0
                    sub_mask  = cmt.CUDAMatrix(sub_mask)
                    mask.mult(sub_mask)
                    del sub_mask
                else:
                    idx = numpy.where(numpy.logical_or(b[:n_tm,:-1] < b[:n_tm,1:] * (term_1 == term_2), (term_1 != term_2)))
                    mask[idx] = 0

            min_time     = local_peaktimes.min()
            max_time     = local_peaktimes.max()
            local_len    = max_time - min_time + 1
            nb_fitted    = 0
            min_times    = numpy.maximum(local_peaktimes - min_time - temp_2_shift, 0)
            max_times    = numpy.minimum(local_peaktimes - min_time + temp_2_shift + 1, max_time-min_time)
            max_n_t      = int(space_explo*(max_time-min_time+1)/(2*temp_2_shift + 1))

            while (numpy.mean(failure) < nb_chances):

                if full_gpu:
                    sub_b       = b.get_row_slice(0, n_tm)
                    sub_b.mult(mask, data)
                    tmp_mat     = data.max(0)
                    argmax_bi   = numpy.argsort(tmp_mat.asarray()[0, :])[::-1]
                    del tmp_mat, sub_b
                else:
                    data        = sub_b * mask
                    argmax_bi   = numpy.argsort(numpy.max(data, 0))[::-1]

                while (len(argmax_bi) > 0):

                    subset          = []
                    indices         = []
                    all_times       = numpy.zeros(local_len, dtype=numpy.bool)

                    for count, idx in enumerate(argmax_bi):
                        myslice = all_times[min_times[idx]:max_times[idx]]
                        if not myslice.any():
                            subset  += [idx]
                            indices += [count]
                            all_times[min_times[idx]:max_times[idx]] = True
                        if len(subset) > max_n_t:
                            break

                    subset    = numpy.array(subset, dtype=numpy.int32)
                    argmax_bi = numpy.delete(argmax_bi, indices)

                    if full_gpu:
                        sub_b             = b.get_row_slice(0, n_tm)
                        tmp_mat           = sub_b.argmax(0)
                        inds_t, inds_temp = subset, tmp_mat.asarray()[0, :][subset].astype(numpy.int32)
                        del tmp_mat
                    else:
                        inds_t, inds_temp = subset, numpy.argmax(sub_b[:, subset], 0)

                    if refractory > 0:
                        sort_idx  = numpy.argsort(inds_t)
                        inds_t    = inds_t[sort_idx]
                        inds_temp = inds_temp[sort_idx]

                    if full_gpu:
                        best_amp  = sub_b.asarray()[inds_temp, inds_t]/n_scalar
                        sub_mask  = numpy.ones((sub_b.shape), dtype=numpy.float32)
                        sub_mask[inds_temp, inds_t] = 0
                        sub_mask  = cmt.CUDAMatrix(sub_mask)
                        mask.mult(sub_mask)
                        del sub_mask
                    else:
                        mask[inds_temp, inds_t] = 0
                        best_amp = sub_b[inds_temp, inds_t]/n_scalar

                    best_amp_n   = best_amp/norm_templates[inds_temp]
                    all_idx      = ((best_amp_n >= amp_limits[inds_temp, 0]) & (best_amp_n <= amp_limits[inds_temp, 1]))
                    to_keep      = numpy.where(all_idx == True)[0]
                    to_reject    = numpy.where(all_idx == False)[0]
                    ts           = local_peaktimes[inds_t[to_keep]]
                    inds_temp_2  = (inds_temp[to_keep] + n_tm)
                    good         = (ts >= local_bounds[0]) & (ts < local_bounds[1])

                    tmp          = numpy.dot(numpy.ones((len(ts), 1), dtype=numpy.int32), local_peaktimes.reshape((1, n_t)))
                    tmp         -= ts.reshape((len(ts), 1))
                    x, y         = numpy.where(numpy.abs(tmp) <= temp_2_shift)
                    itmp         = tmp[x, y].astype(numpy.int32) + temp_2_shift


                    for count, keep, t in zip(range(len(to_keep)), to_keep, ts):

                        if full_gpu:
                            myslice  = x == count
                            idx_b    = y[myslice]
                            cu_slice = cmt.CUDAMatrix(itmp[myslice].reshape(1, len(itmp[myslice])))
                            c        = cmt.empty((N_over, len(itmp[myslice])))
                            if patch_gpu:
                                b_lines  = b.get_col_slice(0, b.shape[0])
                            else:
                                b_lines  = b.get_col_slice(idx_b[0], idx_b[-1]+1)
                            c_overs[inds_temp[keep]].select_columns(cu_slice, c)
                            c.mult_by_scalar(best_amp[keep])
                            b_lines.add(c)
                            if patch_gpu:
                                sub_mat   = b.get_col_slice(0, b.shape[0])
                            else:
                                sub_mat   = b.get_col_slice(inds_t[keep], inds_t[keep]+1)
                            best_amp2 = sub_mat.asarray()[inds_temp_2[count],0]/n_scalar
                            c_overs[inds_temp_2[count]].select_columns(cu_slice, c)
                            c.mult_by_scalar(best_amp2)
                            b_lines.add(c)
                            del cu_slice, b_lines, sub_mat, c
                        else:
                            myslice      = x == count
                            idx_b        = y[myslice]
                            b[:, idx_b] -= best_amp[keep]*c_overlap[inds_temp[keep], :,  itmp[myslice]].T
                            best_amp2    = b[inds_temp_2[count], inds_t[keep]]/n_scalar
                            b[:, idx_b] -= best_amp2*c_overlap[inds_temp_2[count], :,  itmp[myslice]].T

                        if good[count]:
                            normed_2 = norm_templates[inds_temp_2[count]]
                            t_spike  = t + local_offset
                            result['spiketimes'] += [t_spike]
                            result['amplitudes'] += [(best_amp_n[keep], best_amp2/normed_2)]
                            result['templates']  += [inds_temp[keep]]
                            nb_fitted            += 1
                            if refractory > 0:
                                last_spike                   = last_spikes[inds_temp[keep]]
                                sidx                         = numpy.where(all_spikes >= t_spike)[0]
                                last_spikes[inds_temp[keep]] = t_spike
                                values                       = numpy.ones(n_t)
                                values[sidx]                -= numpy.exp((t_spike - all_spikes[sidx])/refractory)
                                if full_gpu:
                                    values   = cmt.CUDAMatrix(values.reshape(1, n_t))
                                    sub_mask = mask.get_row_slice(inds_temp[keep], inds_temp[keep]+1)
                                    sub_mask.mult(values)
                                    mask.set_row_slice(inds_temp[keep], inds_temp[keep]+1, sub_mask)
                                    del values, sub_mask
                                else:
                                    mask[inds_temp[keep]] = mask[inds_temp[keep]] * values

                    myslice           = inds_t[to_reject]
                    failure[myslice] += 1
                    sub_idx           = numpy.where(failure[myslice] >= nb_chances)[0]
                    if full_gpu:
                        N = len(sub_idx)
                        if N > 0:
                            cu_slice = cmt.CUDAMatrix(myslice[sub_idx].reshape(1, N))
                            mask.set_selected_columns(cu_slice, cm_zeros)
                            del cu_slice
                    else:
                        mask[:, myslice[sub_idx]]  = 0

                    if full_gpu:
                        del sub_b

            spikes_to_write     = numpy.array(result['spiketimes'], dtype=numpy.int32)
            amplitudes_to_write = numpy.array(result['amplitudes'], dtype=numpy.float32)
            templates_to_write  = numpy.array(result['templates'], dtype=numpy.int32)

            spiketimes_file.write(spikes_to_write.tostring())
            amplitudes_file.write(amplitudes_to_write.tostring())
            templates_file.write(templates_to_write.tostring())

            if full_gpu:
                del mask, b, cm_zeros, data

        if comm.rank == 0:
            pbar.update(gcount)

    spiketimes_file.close()
    amplitudes_file.close()
    templates_file.close()

    comm.Barrier()

    if comm.rank == 0:
        pbar.finish()

    if comm.rank == 0:
        io.collect_data(comm.size, params, erase=True)
