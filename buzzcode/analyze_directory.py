from buzzcode.utils.analysis import framelength, solve_memory, get_gaps, get_coverage, gaps_to_chunklist, loadup, get_yamnet, load_audio, extract_embeddings, analyze_embeddings
import tensorflow as tf
import pandas as pd
import os
import re
import sys
import librosa
import multiprocessing
import soundfile as sf
from datetime import datetime
from buzzcode.utils.tools import search_dir, Timer, clip_name


tf.config.threading.set_inter_op_parallelism_threads(1)
tf.config.threading.set_intra_op_parallelism_threads(1)

# modelname = "invProp_strict"; cpus=4; memory_allot = 3; dir_raw="./audio_in"; dir_out=None; verbosity=1; conflict_out="quit"; paths_raw = None; pad=False; semantic = True
def analyze_batch(modelname, cpus, memory_allot, semantic = True, dir_raw="./audio_in", paths_raw = None, dir_out=None, verbosity=1, pad=False):
    timer_total = Timer()

    dir_model = os.path.join("models", modelname)

    if dir_out is None:
        dir_out = os.path.join(dir_model, "output")

    chunklength, n_analyzers = solve_memory(memory_allot, cpus)

    log_timestamp = timer_total.time_start.strftime("%Y-%m-%d_%H%M%S")
    path_log = os.path.join(dir_out, f"log {log_timestamp}.txt")
    os.makedirs(os.path.dirname(path_log), exist_ok=True)
    log = open(path_log, "x")
    log.close()

    if paths_raw is None:
        paths_raw = search_dir(dir_raw, list(sf.available_formats().keys()))

    # start logger early and make these exit prints printlogs?
    if len(paths_raw) == 0:
        print(
            f"no compatible audio files found in raw directory {dir_raw} \n"
            f"audio format must be compatible with soundfile module version {sf.__version__} \n"
            "exiting analysis"
        )
        sys.exit(0)

    raws_chunklist = []
    raws_unfinished = []

    for path in paths_raw:
        audio_duration = librosa.get_duration(path=path)

        coverage = get_coverage(path, dir_raw, dir_out)
        if len(coverage) == 0:
            coverage = [(0, 0)]

        gaps = get_gaps((0, audio_duration), coverage)

        # if there's no padding, ignore gaps that start less than 1 frame from file end
        if not pad:
            gaps = [gap for gap in gaps if gap[0] < (audio_duration - (framelength/1000))]

        # expand gaps smaller than one frame; leave gaps larger than one frame
        gaps = [(gap[0], gap[0] + (framelength/1000)) if (gap[1] - gap[0]) < (framelength/1000) else gap for gap in gaps]

        chunklist = gaps_to_chunklist(gaps, chunklength)

        if len(chunklist) > 0:
            raws_unfinished.append(path)
            raws_chunklist.append(chunklist)

    if len(raws_unfinished) == 0:
        print(f"all files in {dir_raw} are fully analyzed; exiting analysis")
        return



    # process control
    #
    dict_chunk = dict(zip(raws_unfinished, raws_chunklist))

    analyzer_ids = list(range(n_analyzers))
    analyzers_per_raw = (n_analyzers/len(raws_unfinished)).__ceil__()
    # if more analyzers than raws, repeat the list, wrapping the assignment back to the start
    dict_analyzer = {i: (raws_unfinished*analyzers_per_raw)[i] for i in analyzer_ids}

    dict_rawstatus = {p: "finished" for p in paths_raw}
    for p in raws_unfinished:
        dict_rawstatus[p] = "not finished"

    q_request = multiprocessing.Queue()
    q_analyze = [multiprocessing.Queue() for _ in analyzer_ids]
    q_write = multiprocessing.Queue()
    q_log = multiprocessing.Queue()

    def printlog(item, item_verb=0):
        time_current = datetime.now()
        q_log.put(f"{time_current} - {item} \n")

        if item_verb <= verbosity:
            print(item)

        return item

    # worker definition
    #
    def worker_manager():
        chunks_remaining = sum([len(c) for c in dict_chunk.values()])

        while chunks_remaining > 0:
            printlog(f"manager: chunks remaining: {chunks_remaining}", 0)

            id_analyzer = q_request.get(block=True)

            path_current = dict_analyzer[id_analyzer]

            # if the file is marked not finished, keep on the current path
            if dict_rawstatus[path_current] == "not finished":
                path_used = path_current
                msg = "continuing on raw"
            # if the file is marked finished
            else:
                # find the worker counts on unfinished files
                workercounts = {p: list(dict_analyzer.values()).count(p) for p in dict_rawstatus if dict_rawstatus[p] != "finished"}

                # assign the first path with fewest workers
                path_used = [p for p in workercounts.keys() if workercounts[p] <= min(workercounts.values())][0]

                # and update worker dict
                dict_analyzer[id_analyzer] = path_used

                msg = "assigned to new raw"

            chunk_out = dict_chunk[path_used].pop(0)
            # if you took the last chunk, mark the file finished
            if len(dict_chunk[path_used]) == 0:
                dict_rawstatus[path_used] = "finished"

            path_clip = clip_name(path_used, dir_raw)
            printlog(f"manager: analyzer {id_analyzer} {msg} {path_clip}, chunk {round(chunk_out[0], 1), round(chunk_out[1], 1)}", 2)

            assignment = (path_used, chunk_out)

            q_analyze[id_analyzer].put(assignment)
            chunks_remaining = sum([len(c) for c in dict_chunk.values()])

        printlog(f"manager: all chunks assigned, queuing terminate signal for analyzers", 2)
        for q in q_analyze:
            q.put("terminate")


    def worker_analyzer(id_analyzer):
        printlog(f"analyzer {id_analyzer}: launching", 1)

        # ready model
        #
        yamnet = get_yamnet()
        model, classes, classes_semantic = loadup(modelname)

        if semantic:
            classes = classes_semantic

        colnames_out = []
        if not semantic:
            colnames_out = ["score_" + c for c in classes]

        columns_desired = ['start', 'end', 'class_predicted', 'score_predicted'] + colnames_out

        q_request.put(id_analyzer)
        assignment = q_analyze[id_analyzer].get()

        timer_analysis = Timer()
        while assignment != "terminate":
            timer_analysis.restart()
            path_raw = assignment[0]
            path_clip = clip_name(path_raw, dir_raw)
            time_from = assignment[1][0]
            time_to = assignment[1][1]
            chunk_duration = time_to - time_from

            printlog(f"analyzer {id_analyzer}: analyzing {path_clip} from {round(time_from, 1)}s to {round(time_to, 1)}s", 1)
            audio_data = load_audio(path_raw, time_from, time_to)
            embeddings = extract_embeddings(audio_data, yamnet)
            results = analyze_embeddings(model=model, classes=classes, embeddings=embeddings)

            results['start'] = results['start'] + time_from
            results['end'] = results['end'] + time_from

            results = results[columns_desired]

            q_write.put((path_raw, results))
            q_request.put(id_analyzer)

            timer_analysis.stop()
            analysis_rate = (chunk_duration / timer_analysis.get_total()).__round__(1)
            printlog(
                f"analyzer {id_analyzer}: analyzed {path_clip} from {round(time_from, 1)}s to {round(time_to, 1)}s in {timer_analysis.get_total()}s (rate: {analysis_rate})",
                1)

            assignment = q_analyze[id_analyzer].get()
            
        printlog(f"analyzer {id_analyzer}: terminating")
        q_write.put(("terminate", id_analyzer))  # not super happy with this; feels a bit hacky
        sys.exit(0)

    def worker_writer():
        printlog(f"writer: initialized", 2)

        dirs_raw = set([os.path.dirname(p) for p in paths_raw])
        dirs_out = [re.sub(dir_raw, dir_out, d) for d in dirs_raw]
        for d in dirs_out:
            os.makedirs(d, exist_ok=True)

        status_analyzers = [True for _ in analyzer_ids]
        while True in status_analyzers:
            path_raw, results = q_write.get()

            if path_raw == "terminate":
                status_analyzers[results] = False
                continue

            path_out = os.path.splitext(path_raw)[0] + '_buzzdetect.csv'
            path_out = re.sub(dir_raw, dir_out, path_out)
            path_clip = clip_name(path_out, dir_out)

            if os.path.exists(path_out):
                printlog(f"writer: updating file for {path_clip}", 2)
                results_written = pd.read_csv(path_out)
                results_updated = pd.concat([results_written, results],axis=0, ignore_index=True)
                results_updated = results_updated.sort_values(by = "start")

                results_updated.to_csv(path_out, index = False)
            else:
                printlog(f"writer: creating new file for {path_clip}", 2)
                results.to_csv(path_out)

        printlog(f"writer: terminating")
        q_log.put("terminate")

    def worker_logger():
        log_item = q_log.get(block=True)
        while log_item != "terminate":
            file_log = open(path_log, "a")
            file_log.write(log_item)
            file_log.close()
            log_item = q_log.get(block=True)

        timer_total.stop()
        closing_message = f"{datetime.now()} - analysis complete; total time: {timer_total.get_total()}s"

        print(closing_message)
        file_log = open(path_log, "a")
        file_log.write(closing_message)
        file_log.close()

    # Go!
    #
    printlog(
        f"begin analysis \n"
        f"start time: {timer_total.time_start} \n"
        f"model: {modelname}\n"
        f"CPU count: {cpus}\n"
        f"memory allotment {memory_allot}\n",
        0)

    # launch analysis_process; will wait immediately
    proc_analyzers = []
    for a in range(n_analyzers):
        proc_analyzers.append(
            multiprocessing.Process(target=worker_analyzer, name=f"analysis_proc{a}", args=([a])))
        proc_analyzers[-1].start()
        pass

    proc_logger = multiprocessing.Process(target=worker_logger)
    proc_logger.start()

    proc_writer = multiprocessing.Process(target=worker_writer)
    proc_writer.start()

    proc_manager = multiprocessing.Process(target=worker_manager())
    proc_manager.start()

    # wait for analysis to finish
    proc_logger.join()


if __name__ == "__main__":
    modelname = "currentbest"
    analyze_batch(modelname=modelname, cpus=6, memory_allot=8, verbosity=2, semantic=True)
