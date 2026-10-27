"""Conservative local nearest-example recognition; no semantic training claims."""
import numpy as np

VERSION = 'mel32-time24-v1'
THRESHOLD = .94
MARGIN = .04
SIZE = 32 * 24 + 32 * 2


def audio_feature(pcm):
    if not pcm or len(pcm) % 2 or len(pcm) > 1920000:
        return None
    audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
    if len(audio) < 6400 or np.sqrt(np.mean(audio**2)) < .003:
        return None
    audio -= audio.mean()
    # Trim quiet edges, retaining temporal structure within each call.
    active = np.flatnonzero(np.abs(audio) >= max(.003, np.max(np.abs(audio)) * .08))
    if len(active) < 800: return None
    audio = audio[max(0, active[0]-320):min(len(audio), active[-1]+321)]
    if len(audio) < 400: return None
    frames = np.lib.stride_tricks.sliding_window_view(audio, 400)[::160]
    power = np.abs(np.fft.rfft(frames * np.hanning(400), n=512)) ** 2
    frequencies = np.fft.rfftfreq(512, 1/16000)
    mel = lambda f: 2595 * np.log10(1+f/700)
    edges = 700 * (10**(np.linspace(mel(100), mel(7500), 34)/2595)-1)
    filters = np.maximum(0, np.minimum(
        (frequencies[None, :] - edges[:-2, None]) / (edges[1:-1]-edges[:-2])[:, None],
        (edges[2:, None] - frequencies[None, :]) / (edges[2:]-edges[1:-1])[:, None]))
    energy = power @ filters.T
    peak = float(energy.max())
    if peak <= 1e-9: return None
    spectrum = np.clip((10*np.log10(np.maximum(energy/peak, 1e-6)) + 60)/60, 0, 1)
    # Fixed time bins support differing durations without storing raw waveforms in RAM.
    timeline = np.linspace(0, len(spectrum)-1, 24)
    pooled = np.stack([np.interp(timeline, np.arange(len(spectrum)), spectrum[:, i]) for i in range(32)], axis=1)
    feature = np.concatenate([pooled.ravel(), spectrum.mean(axis=0)*3, spectrum.std(axis=0)*3])
    feature -= feature.mean()
    norm = np.linalg.norm(feature)
    if norm < 1e-6: return None
    return (feature/norm).astype('<f4')


class ExampleRecognizer:
    def __init__(self, store): self.store = store

    def recognize(self, pcm, fallback, exclude_id=None):
        if fallback.get('label') in ('声音过轻', '录音过短', '人声'):
            return fallback
        query = audio_feature(pcm)
        if query is None: return fallback
        sequence = audio_sequence(pcm)
        examples = self.store.examples(exclude_id)
        candidates = []
        for item in examples:
            feature = None
            reference_sequence = None
            if item['feature_version'] == SEQUENCE_VERSION and item['feature']:
                cached = np.frombuffer(item['feature'], dtype='<f4')
                if len(cached) >= SIZE and (len(cached)-SIZE) % 32 == 0 and np.isfinite(cached).all():
                    feature = cached[:SIZE]
                    tail = cached[SIZE:]
                    if len(tail): reference_sequence = tail.reshape(-1,32)
            if feature is None:
                reference = self.store.pcm(item['id'])
                feature = audio_feature(reference)
                reference_sequence = audio_sequence(reference)
                if feature is not None:
                    cached = feature if reference_sequence is None else np.concatenate([feature,reference_sequence.ravel()])
                    self.store.cache_feature(item['id'],cached.astype('<f4').tobytes(),SEQUENCE_VERSION)
            if feature is None: continue
            global_score = float(np.dot(query,feature))
            aligned_score = aligned_similarity(sequence,reference_sequence)
            candidates.append({'global': global_score, 'aligned': aligned_score, 'item': item})
        if not candidates: return fallback
        # Each independent matcher must pass its own threshold and conflicting-label margin.
        winner = None
        for metric, threshold, margin in [('global',THRESHOLD,MARGIN),
                                           ('aligned',SEQUENCE_THRESHOLD,SEQUENCE_MARGIN)]:
            ranked = sorted(candidates,key=lambda candidate:candidate[metric],reverse=True)
            best = ranked[0]
            rival = next((c[metric] for c in ranked if c['item']['label'] != best['item']['label']),-1)
            if best[metric] >= threshold and best[metric]-rival >= margin:
                if winner is not None and winner['item']['label'] != best['item']['label']:
                    winner = None
                    break
                winner = {**best,'metric':metric,'score':best[metric]}
        diagnostic = {'best_global':round(max(c['global'] for c in candidates),4),
                      'best_aligned':round(max(c['aligned'] for c in candidates),4),
                      'sample_count':len(candidates)}
        if winner is None:
            return {**fallback,'match_diagnostics':{**diagnostic,'reason':'below_threshold_or_conflicting_labels'},
                    'note':'本次未找到足够明确的人工标注匹配，显示声音类型提示。可在后台标注这条新录音。'}
        best = winner['item']
        current = {item['id']:item['label'] for item in self.store.examples(exclude_id)}
        if current != {item['id']:item['label'] for item in examples}:
            return fallback
        return {**fallback,'label':best['label'],
                'text':f"猫叫识别：{best['label']}",
                'method':'本地人工样本匹配（声学特征 + 时间对齐）+ YAMNet',
                'note':'参考已标注猫叫；为样本匹配结果，需结合现场情境判断',
                'recognition_source':'annotated_example','example_id':best['id'],
                'similarity':round(winner['score'],4),'match_method':winner['metric'],
                'match_diagnostics':{**diagnostic,'reason':'matched'},'model_prediction':fallback}


SEQUENCE_VERSION = 'mel32-cmvn-sequence-v2'
SEQUENCE_THRESHOLD = .90
SEQUENCE_MARGIN = .02


def audio_sequence(pcm):
    """Active log-mel frames with channel normalization for speaker/mic replay."""
    if not pcm or len(pcm) % 2 or len(pcm) > 1920000: return None
    audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
    if len(audio) < 6400 or np.sqrt(np.mean(audio**2)) < .003: return None
    audio -= audio.mean()
    frames = np.lib.stride_tricks.sliding_window_view(audio, 400)[::160]
    rms = np.sqrt(np.mean(frames**2, axis=1))
    active = rms > max(.003, float(rms.max())*.13)
    if active.sum() < 40: return None
    power = abs(np.fft.rfft(frames[active]*np.hanning(400), n=512))**2
    frequencies = np.fft.rfftfreq(512, 1/16000)
    mel = lambda f: 2595*np.log10(1+f/700)
    edges = 700*(10**(np.linspace(mel(100), mel(7500), 34)/2595)-1)
    filters = np.maximum(0, np.minimum(
        (frequencies[None,:]-edges[:-2,None])/(edges[1:-1]-edges[:-2])[:,None],
        (edges[2:,None]-frequencies[None,:])/(edges[2:]-edges[1:-1])[:,None]))
    spectrum = 10*np.log10(np.maximum(power@filters.T, 1e-8))
    pooled = np.array([chunk.mean(axis=0) for chunk in np.array_split(
        spectrum, max(1, min(240, len(spectrum)//5)))])
    pooled -= pooled.mean(axis=0, keepdims=True)
    pooled -= pooled.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(pooled, axis=1, keepdims=True)
    if np.count_nonzero(norms > 1e-4) < 8: return None
    return (pooled/np.maximum(norms, 1e-9)).astype('<f4')


def aligned_similarity(a, b):
    """Subsequence DTW: align the shorter call, requiring meaningful coverage."""
    if a is None or b is None: return 0.0
    if len(a) > len(b): a, b = b, a
    cost = np.clip(1-a@b.T, 0, 2)
    n, m = cost.shape
    distances = np.full((n+1, m+1), np.inf)
    distances[0,:] = 0
    lengths = np.zeros((n+1, m+1), dtype=np.int32)
    starts = np.zeros((n+1, m+1), dtype=np.int32)
    starts[0] = np.arange(m+1)
    for i in range(1, n+1):
        for j in range(1, m+1):
            previous = ((i-1,j-1),(i-1,j),(i,j-1))
            k,l = min(previous, key=lambda index: distances[index])
            distances[i,j] = distances[k,l]+cost[i-1,j-1]
            lengths[i,j] = lengths[k,l]+1
            starts[i,j] = starts[k,l]
    scores = [1-distances[n,j]/lengths[n,j] for j in range(1,m+1)
              if j-starts[n,j] >= max(n*.5,m*.45)]
    return float(max(scores, default=0))
