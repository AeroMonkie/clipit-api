"""
ClipIt - Video Copyright Scanner & Clipper
Upload a video, scan for copyrighted music, clip segments with audio removed.
"""

import os
import json
import time
import hmac
import base64
import hashlib
import tempfile
import subprocess
import shutil
import threading
import uuid
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)

# Configuration
AUDD_API_TOKEN = os.environ.get('AUDD_API_TOKEN', '')
ACRCLOUD_HOST = os.environ.get('ACRCLOUD_HOST', '').replace('https://', '').replace('http://', '').rstrip('/')
ACRCLOUD_ACCESS_KEY = os.environ.get('ACRCLOUD_ACCESS_KEY', '')
ACRCLOUD_ACCESS_SECRET = os.environ.get('ACRCLOUD_ACCESS_SECRET', '')
MUSIC_RECOGNITION_PROVIDER = os.environ.get('MUSIC_RECOGNITION_PROVIDER', '').lower()

def get_int_env(name: str, default: int, *, minimum: int = 0) -> int:
    """Read a positive-ish integer env var with a safe fallback."""
    raw = os.environ.get(name, '')
    if not raw:
        return default
    try:
        value = int(raw)
        return value if value >= minimum else default
    except ValueError:
        return default


# Chunk configuration for audio analysis. Keep conservative defaults for a
# synchronous request/response API; tighter settings can be enabled via Railway
# env vars once the scan path is made asynchronous or parallelized safely.
CHUNK_DURATION = get_int_env('CHUNK_DURATION', 12, minimum=5)  # seconds per chunk
OVERLAP = get_int_env('OVERLAP', 4, minimum=0)  # seconds overlap between chunks
MERGE_GAP = get_int_env('MERGE_GAP', 30, minimum=0)  # seconds gap tolerance for merging same-song detections
JOB_TTL_SECONDS = get_int_env('JOB_TTL_SECONDS', 3600, minimum=300)

# Prevent bad env values from creating a zero/negative scan stride.
if OVERLAP >= CHUNK_DURATION:
    OVERLAP = max(0, CHUNK_DURATION - 1)


# In-memory scan jobs. This is intentionally simple: Railway restarts will clear
# active jobs, but that is acceptable for this lightweight single-service tool.
SCAN_JOBS = {}
SCAN_JOBS_LOCK = threading.Lock()


def get_media_duration(file_path: str) -> float:
    """Get duration of media file using ffprobe."""
    cmd = [
        'ffprobe',
        '-v', 'quiet',
        '-show_entries', 'format=duration',
        '-of', 'json',
        file_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"ffprobe failed: {result.stderr}")
    
    data = json.loads(result.stdout)
    return float(data['format']['duration'])


def extract_audio_from_video(video_path: str, audio_path: str):
    """Extract audio track from video file."""
    cmd = [
        'ffmpeg',
        '-y',
        '-i', video_path,
        '-vn',
        '-acodec', 'libmp3lame',
        '-ar', '44100',
        '-ac', '1',
        '-b:a', '128k',
        audio_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        # Video might not have audio track
        return False
    return True


def extract_audio_chunk(audio_path: str, start_time: float, duration: float, output_path: str):
    """Extract a chunk of audio using ffmpeg."""
    cmd = [
        'ffmpeg',
        '-y',
        '-i', audio_path,
        '-ss', str(start_time),
        '-t', str(duration),
        '-acodec', 'libmp3lame',
        '-ar', '44100',
        '-ac', '1',
        '-b:a', '128k',
        output_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise Exception(f"ffmpeg failed: {result.stderr}")


def recognize_with_audd(audio_path: str) -> dict:
    """Recognize music using AudD API."""
    if not AUDD_API_TOKEN:
        return {'error': 'AudD API token not configured'}
    
    with open(audio_path, 'rb') as f:
        data = {
            'api_token': AUDD_API_TOKEN,
            'return': 'timecode,spotify'
        }
        files = {'file': f}
        
        response = requests.post(
            'https://api.audd.io/',
            data=data,
            files=files,
            timeout=30
        )
    
    return response.json()


def acrcloud_configured() -> bool:
    """Return True when ACRCloud credentials are configured."""
    return bool(ACRCLOUD_HOST and ACRCLOUD_ACCESS_KEY and ACRCLOUD_ACCESS_SECRET)


def active_recognition_provider() -> str:
    """Choose the recognition provider without breaking existing deployments.

    If MUSIC_RECOGNITION_PROVIDER is explicitly set, honor it. Otherwise prefer
    ACRCloud when configured, and fall back to AudD while Railway env vars are
    being cut over.
    """
    if MUSIC_RECOGNITION_PROVIDER in {'acrcloud', 'audd'}:
        return MUSIC_RECOGNITION_PROVIDER
    if acrcloud_configured():
        return 'acrcloud'
    if AUDD_API_TOKEN:
        return 'audd'
    return 'acrcloud'


def recognize_with_acrcloud(audio_path: str) -> dict:
    """Recognize music using ACRCloud Identification API."""
    if not acrcloud_configured():
        return {'error': 'ACRCloud API credentials not configured'}

    http_method = 'POST'
    http_uri = '/v1/identify'
    data_type = 'audio'
    signature_version = '1'
    timestamp = str(time.time())

    string_to_sign = '\n'.join([
        http_method,
        http_uri,
        ACRCLOUD_ACCESS_KEY,
        data_type,
        signature_version,
        timestamp,
    ])
    signature = base64.b64encode(
        hmac.new(
            ACRCLOUD_ACCESS_SECRET.encode('ascii'),
            string_to_sign.encode('ascii'),
            hashlib.sha1,
        ).digest()
    ).decode('ascii')

    with open(audio_path, 'rb') as f:
        response = requests.post(
            f'https://{ACRCLOUD_HOST}{http_uri}',
            data={
                'access_key': ACRCLOUD_ACCESS_KEY,
                'sample_bytes': str(os.path.getsize(audio_path)),
                'timestamp': timestamp,
                'signature': signature,
                'data_type': data_type,
                'signature_version': signature_version,
            },
            files={'sample': (os.path.basename(audio_path), f, 'application/octet-stream')},
            timeout=30,
        )

    try:
        result = response.json()
    except Exception:
        return {'error': f'ACRCloud returned non-JSON response: HTTP {response.status_code}'}

    if response.status_code >= 400:
        return {'error': f'ACRCloud HTTP {response.status_code}', 'response': result}

    return result


def parse_audd_result(result: dict) -> dict:
    """Parse AudD API response."""
    if result.get('status') != 'success' or not result.get('result'):
        return None
    
    track = result['result']
    return {
        'title': track.get('title', 'Unknown'),
        'artists': [track.get('artist', 'Unknown')],
        'album': track.get('album', 'Unknown'),
        'release_date': track.get('release_date', 'Unknown'),
        'label': track.get('label', 'Unknown'),
        'confidence': 100,
    }


def parse_acrcloud_result(result: dict) -> dict:
    """Parse ACRCloud Identification API response into ClipIt's song shape."""
    status = result.get('status') or {}
    if status.get('code') not in (0, '0'):
        return None

    music = (result.get('metadata') or {}).get('music') or []
    if not music:
        return None

    track = music[0]
    artists = [a.get('name') for a in track.get('artists', []) if a.get('name')]
    album = track.get('album') or {}
    release_date = track.get('release_date') or track.get('release_date_original') or 'Unknown'

    return {
        'title': track.get('title', 'Unknown'),
        'artists': artists or ['Unknown'],
        'album': album.get('name', 'Unknown') if isinstance(album, dict) else 'Unknown',
        'release_date': release_date,
        'label': (track.get('label') or 'Unknown'),
        'confidence': track.get('score', 100),
        'acrcloud_id': track.get('acrid'),
    }


def recognize_music(audio_path: str) -> dict:
    """Recognize music with the configured provider."""
    if active_recognition_provider() == 'audd':
        return recognize_with_audd(audio_path)
    return recognize_with_acrcloud(audio_path)


def parse_recognition_result(result: dict) -> dict:
    """Parse recognition response from the configured provider."""
    if active_recognition_provider() == 'audd':
        return parse_audd_result(result)
    return parse_acrcloud_result(result)


def format_timestamp(seconds: float) -> str:
    """Format seconds to MM:SS or HH:MM:SS."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def parse_timestamp(ts: str) -> float:
    """Convert timestamp string (MM:SS or HH:MM:SS) to seconds."""
    ts = ts.strip()
    parts = ts.split(':')
    
    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + float(seconds)
    elif len(parts) == 3:
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    else:
        raise ValueError(f"Invalid timestamp format: {ts}")


def analyze_audio(audio_path: str, max_duration: float = None, progress_callback=None) -> dict:
    """Analyze audio file for copyrighted music."""
    results = {
        'songs': [],
        'analysis_chunks': 0,
        'errors': []
    }
    
    temp_dir = tempfile.mkdtemp()
    
    try:
        duration = get_media_duration(audio_path)
        
        # Apply max_duration limit if set
        analyze_duration = duration
        if max_duration and max_duration < duration:
            analyze_duration = max_duration
            results['scan_mode'] = f'First {format_timestamp(max_duration)}'
        else:
            results['scan_mode'] = 'Full video'
        
        # Calculate chunks
        chunks = []
        current_time = 0
        while current_time < analyze_duration:
            chunk_end = min(current_time + CHUNK_DURATION, analyze_duration)
            chunks.append((current_time, chunk_end - current_time))
            current_time += CHUNK_DURATION - OVERLAP
        
        results['analysis_chunks'] = len(chunks)
        if progress_callback:
            progress_callback('Scanning audio chunks...', 0, len(chunks))
        
        # Analyze each chunk
        detected_songs = {}
        
        for i, (start_time, chunk_duration) in enumerate(chunks):
            chunk_path = os.path.join(temp_dir, f'chunk_{i}.mp3')
            if progress_callback:
                progress_callback(
                    f"Scanning segment {i + 1} of {len(chunks)} ({format_timestamp(start_time)})...",
                    i,
                    len(chunks)
                )
            
            try:
                extract_audio_chunk(audio_path, start_time, chunk_duration, chunk_path)
                
                result = recognize_music(chunk_path)
                parsed = parse_recognition_result(result)
                
                if parsed:
                    song_key = f"{parsed['title']}|{'|'.join(parsed['artists'])}"
                    
                    if song_key not in detected_songs:
                        detected_songs[song_key] = {
                            **parsed,
                            'timestamps': [],
                            'time_ranges': []
                        }
                    
                    detected_songs[song_key]['timestamps'].append(start_time)
                    detected_songs[song_key]['time_ranges'].append({
                        'start': format_timestamp(start_time),
                        'end': format_timestamp(min(start_time + chunk_duration, duration)),
                        'start_seconds': start_time,
                        'end_seconds': min(start_time + chunk_duration, duration)
                    })
                
                if os.path.exists(chunk_path):
                    os.remove(chunk_path)
                    
            except Exception as e:
                results['errors'].append(f"Chunk {i} ({format_timestamp(start_time)}): {str(e)}")

            if progress_callback:
                progress_callback(
                    f"Scanned segment {i + 1} of {len(chunks)}",
                    i + 1,
                    len(chunks)
                )
        
        # Merge consecutive time ranges for each song
        for song_key, song_data in detected_songs.items():
            merged_ranges = []
            ranges = sorted(song_data['time_ranges'], key=lambda x: x['start_seconds'])
            
            for r in ranges:
                if merged_ranges and r['start_seconds'] <= merged_ranges[-1]['end_seconds'] + MERGE_GAP:
                    merged_ranges[-1]['end_seconds'] = max(merged_ranges[-1]['end_seconds'], r['end_seconds'])
                    merged_ranges[-1]['end'] = format_timestamp(merged_ranges[-1]['end_seconds'])
                else:
                    merged_ranges.append(r.copy())
            
            song_data['time_ranges'] = merged_ranges
            results['songs'].append(song_data)
        
        results['songs'].sort(key=lambda x: x['time_ranges'][0]['start_seconds'] if x['time_ranges'] else 0)
        
    except Exception as e:
        results['errors'].append(str(e))
    
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    return results


def clip_video_no_audio(input_path: str, output_path: str, start_time: float, end_time: float):
    """Extract a video segment with audio removed."""
    duration = end_time - start_time
    
    cmd = [
        'ffmpeg',
        '-y',
        '-ss', str(start_time),
        '-i', input_path,
        '-t', str(duration),
        '-c:v', 'copy',
        '-an',  # Remove audio
        '-movflags', '+faststart',
        '-avoid_negative_ts', 'make_zero',
        output_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise Exception(f"ffmpeg failed: {result.stderr}")


def prune_scan_jobs():
    """Remove old completed/failed jobs from memory."""
    cutoff = time.time() - JOB_TTL_SECONDS
    with SCAN_JOBS_LOCK:
        old_job_ids = [
            job_id for job_id, job in SCAN_JOBS.items()
            if job.get('status') in {'complete', 'error'} and job.get('updated_at', 0) < cutoff
        ]
        for job_id in old_job_ids:
            SCAN_JOBS.pop(job_id, None)


def update_scan_job(job_id: str, **updates):
    """Thread-safe scan job update."""
    with SCAN_JOBS_LOCK:
        job = SCAN_JOBS.get(job_id)
        if not job:
            return
        job.update(updates)
        job['updated_at'] = time.time()


def get_scan_job(job_id: str):
    """Thread-safe scan job snapshot."""
    with SCAN_JOBS_LOCK:
        job = SCAN_JOBS.get(job_id)
        return dict(job) if job else None


def run_scan_job(job_id: str, filename: str, file_ext: str, video_path: str, audio_path: str, temp_dir: str, max_duration: float = None):
    """Background worker for an uploaded scan."""
    try:
        update_scan_job(job_id, status='processing', message='Reading video metadata...', progress=5)
        duration = get_media_duration(video_path)

        update_scan_job(job_id, message='Extracting audio...', progress=10, video_duration=duration, video_duration_formatted=format_timestamp(duration))
        has_audio = extract_audio_from_video(video_path, audio_path)

        if not has_audio:
            update_scan_job(job_id, status='complete', message='Complete', progress=100, result={
                'filename': filename,
                'video_duration': duration,
                'video_duration_formatted': format_timestamp(duration),
                'songs': [],
                'analysis_chunks': 0,
                'scan_mode': 'N/A',
                'errors': ['Video has no audio track']
            })
            return

        def on_progress(message: str, done: int, total: int):
            # Reserve 0-15% for setup and 95-100% for merge/final response.
            chunk_progress = 15
            if total:
                chunk_progress = 15 + int((done / total) * 80)
            update_scan_job(job_id, message=message, progress=min(95, chunk_progress), chunks_done=done, chunks_total=total)

        results = analyze_audio(audio_path, max_duration=max_duration, progress_callback=on_progress)
        results['filename'] = filename
        results['video_duration'] = duration
        results['video_duration_formatted'] = format_timestamp(duration)

        update_scan_job(job_id, status='complete', message='Complete', progress=100, result=results)

    except Exception as e:
        update_scan_job(job_id, status='error', message=str(e), error=str(e), progress=100)

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.route('/')
def index():
    return jsonify({'status': 'ClipIt API is running', 'version': '1.0'})


@app.route('/api/scan', methods=['POST'])
def scan():
    """Scan uploaded video for copyrighted music.

    By default this preserves the original synchronous response contract. The
    updated frontend sends async=1, which starts a background scan job and polls
    /api/scan-status/<job_id> so long full-video scans do not time out.
    """
    prune_scan_jobs()
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    allowed_extensions = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v', '.wmv', '.flv'}
    file_ext = os.path.splitext(file.filename)[1].lower()
    
    if file_ext not in allowed_extensions:
        return jsonify({'error': f'Invalid file type. Allowed: {", ".join(allowed_extensions)}'}), 400
    
    temp_dir = tempfile.mkdtemp()
    video_path = os.path.join(temp_dir, f'input{file_ext}')
    audio_path = os.path.join(temp_dir, 'audio.mp3')
    
    try:
        file.save(video_path)

        max_duration = request.form.get('max_duration', None)
        if max_duration:
            max_duration = float(max_duration)

        async_requested = request.form.get('async') == '1' or request.form.get('async_scan') == '1'
        if not async_requested:
            duration = get_media_duration(video_path)
            has_audio = extract_audio_from_video(video_path, audio_path)

            if not has_audio:
                return jsonify({
                    'filename': file.filename,
                    'video_duration': duration,
                    'video_duration_formatted': format_timestamp(duration),
                    'songs': [],
                    'analysis_chunks': 0,
                    'scan_mode': 'N/A',
                    'errors': ['Video has no audio track']
                })

            results = analyze_audio(audio_path, max_duration=max_duration)
            results['filename'] = file.filename
            results['video_duration'] = duration
            results['video_duration_formatted'] = format_timestamp(duration)
            return jsonify(results)

        job_id = uuid.uuid4().hex
        now = time.time()
        with SCAN_JOBS_LOCK:
            SCAN_JOBS[job_id] = {
                'job_id': job_id,
                'status': 'queued',
                'message': 'Queued...',
                'progress': 0,
                'filename': file.filename,
                'created_at': now,
                'updated_at': now,
            }

        thread = threading.Thread(
            target=run_scan_job,
            args=(job_id, file.filename, file_ext, video_path, audio_path, temp_dir, max_duration),
            daemon=True,
        )
        thread.start()

        return jsonify({
            'job_id': job_id,
            'status': 'queued',
            'message': 'Scan started',
            'status_url': f'/api/scan-status/{job_id}'
        }), 202
        
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return jsonify({'error': str(e)}), 500

    finally:
        # Async workers own temp_dir cleanup after the scan completes.
        if 'async_requested' in locals() and not async_requested:
            shutil.rmtree(temp_dir, ignore_errors=True)


@app.route('/api/scan-status/<job_id>', methods=['GET'])
def scan_status(job_id):
    """Return async scan job progress or final result."""
    prune_scan_jobs()
    job = get_scan_job(job_id)
    if not job:
        return jsonify({'error': 'Scan job not found or expired'}), 404

    response = {
        'job_id': job_id,
        'status': job.get('status'),
        'message': job.get('message', ''),
        'progress': job.get('progress', 0),
        'filename': job.get('filename'),
        'chunks_done': job.get('chunks_done', 0),
        'chunks_total': job.get('chunks_total', 0),
    }
    if job.get('video_duration_formatted'):
        response['video_duration_formatted'] = job.get('video_duration_formatted')
    if job.get('status') == 'complete':
        response['result'] = job.get('result')
    if job.get('status') == 'error':
        response['error'] = job.get('error') or job.get('message')

    return jsonify(response)


@app.route('/api/clip', methods=['POST'])
def clip():
    """Clip video segment with audio removed."""
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    start_ts = request.form.get('start', '')
    end_ts = request.form.get('end', '')
    
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    if not start_ts or not end_ts:
        return jsonify({'error': 'Start and end timestamps are required'}), 400
    
    allowed_extensions = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v', '.wmv', '.flv'}
    file_ext = os.path.splitext(file.filename)[1].lower()
    
    if file_ext not in allowed_extensions:
        return jsonify({'error': f'Invalid file type. Allowed: {", ".join(allowed_extensions)}'}), 400
    
    try:
        start_seconds = parse_timestamp(start_ts)
        end_seconds = parse_timestamp(end_ts)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    
    if start_seconds >= end_seconds:
        return jsonify({'error': 'Start time must be before end time'}), 400
    
    if start_seconds < 0:
        return jsonify({'error': 'Start time cannot be negative'}), 400
    
    temp_dir = tempfile.mkdtemp()
    input_path = os.path.join(temp_dir, f'input{file_ext}')
    output_path = os.path.join(temp_dir, 'output.mp4')
    
    try:
        file.save(input_path)
        
        duration = get_media_duration(input_path)
        
        if end_seconds > duration:
            return jsonify({'error': f'End time ({end_ts}) exceeds video duration ({format_timestamp(duration)})'}), 400
        
        clip_video_no_audio(input_path, output_path, start_seconds, end_seconds)
        
        original_name = os.path.splitext(file.filename)[0]
        output_filename = f"{original_name}_clip_{start_ts.replace(':', '-')}_to_{end_ts.replace(':', '-')}_noaudio.mp4"
        
        return send_file(
            output_path,
            as_attachment=True,
            download_name=output_filename,
            mimetype='video/mp4'
        )
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
        
    finally:
        if os.path.exists(input_path):
            os.remove(input_path)


@app.route('/api/config', methods=['GET'])
def get_config():
    """Check API status."""
    acr_ready = acrcloud_configured()
    provider = active_recognition_provider()
    provider_ready = bool(AUDD_API_TOKEN) if provider == 'audd' else acr_ready
    return jsonify({
        'status': 'ready' if provider_ready else 'not_configured',
        # Kept for the existing GoDaddy frontend, which checks audd_configured.
        'audd_configured': provider_ready,
        'acrcloud_configured': acr_ready,
        'recognition_provider': provider,
        'scan_settings': {
            'chunk_duration_seconds': CHUNK_DURATION,
            'overlap_seconds': OVERLAP,
            'scan_step_seconds': CHUNK_DURATION - OVERLAP,
            'merge_gap_seconds': MERGE_GAP,
        },
        'max_file_size': '100MB',
        'supported_formats': ['mp4', 'mov', 'avi', 'mkv', 'webm', 'm4v', 'wmv', 'flv']
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
