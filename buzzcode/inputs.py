from buzzcode.utils import search_dir
from buzzcode.audio import load_audio, frame_audio
from buzzcode.embeddings import get_embedder
from buzzcode.audio import extract_frequencies
import soundfile as sf
import tensorflow as tf
import numpy as np
import multiprocessing
import librosa
import re
import os


def extract_input(frames, sr_native, embedder, config):
    # if i always want to use beeband and global, I could roll both into a single dominant_frequencies analysis
    frequencies_beeband = [extract_frequencies(frame, sr=sr_native) for frame in frames]
    frequency_global = [extract_frequencies(frame, sr=sr_native, n_freq=1, freq_range=(0, sr_native)) for frame in frames]

    if sr_native != config['samplerate']:
        frames = [librosa.resample(frame, orig_sr=sr_native, target_sr=config['samplerate']) for frame in frames]

    embeddings = embedder(frames)

    inputs = [np.concatenate((e, fb, fg)) for e, fb, fg in zip(embeddings, frequencies_beeband, frequency_global)]

    return inputs


# cpus=2; dir_in = './training/audio'; dir_out = None; conflict='overwrite'; worker_id=0; embeddername='yamnet'
# WARNING: cannot handle files larger than memory
def cache_input(cpus, dir_in, embeddername='yamnet', conflict='skip', dir_out=None):
    paths_in = search_dir(dir_in, list(sf.available_formats()))
    if len(paths_in) == 0:
        quit("no compatible audio files found in input directory")

    if dir_out is None:
        dir_out = os.path.join(os.path.dirname(dir_in), f'inputCache_{embeddername}')

    paths_out = [re.sub(dir_in, dir_out, path_in) for path_in in paths_in]
    paths_out = [os.path.splitext(path)[0] + '.npy' for path in paths_out]  # change
    dirs_out = set([os.path.dirname(p) for p in paths_out])

    for d in dirs_out:
        os.makedirs(d, exist_ok=True)

    assignments = list(zip(paths_in, paths_out))

    if conflict == 'skip':
        assignments = [assignment for assignment in assignments if not os.path.exists(assignment[1])]

    if len(assignments) == 0:
        quit('cached inputs already exist for every file; quitting')

    q_assignments = multiprocessing.Queue()

    for a in assignments:
        q_assignments.put(a)

    for _ in range(cpus):
        q_assignments.put(('terminate', 'terminate'))

    def worker_cacher(worker_id):
        print(f"cacher {worker_id}: launching")
        tf.config.threading.set_inter_op_parallelism_threads(1)
        tf.config.threading.set_intra_op_parallelism_threads(1)
        embedder, config = get_embedder(embeddername)

        assignment = q_assignments.get()
        path_in = assignment[0]
        path_out = assignment[1]

        while path_in != "terminate":
            print(f"{worker_id}: getting inputs for {re.sub(dir_in, '', path_in)}")
            audio_data, sr_native = load_audio(path_in)

            if len(audio_data)/sr_native < config['framelength']:
                print(f"{worker_id}: skipping inputs for {re.sub(dir_in, '', path_in)}; sub-frame audio segment")

                assignment = q_assignments.get()
                path_in = assignment[0]
                path_out = assignment[1]
                continue

            frames = frame_audio(audio_data, config['framelength'], sr_native)
            inputs = extract_input(frames, sr_native, embedder, config)

            np.save(path_out, inputs)

            assignment = q_assignments.get()
            path_in = assignment[0]
            path_out = assignment[1]

        print(f"{worker_id}: terminating")

    cachers = []
    for c in range(cpus):
        cachers.append(
            multiprocessing.Process(target=worker_cacher, name=f"embedder_{c}", args=([c])))
        cachers[-1].start()

    for c in range(cpus):
        cachers[c].join()

    print('done caching! :)')

if __name__ == '__main__':
    cache_input(8, './training/audio', 'yamnet', conflict='skip')

