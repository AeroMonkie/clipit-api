"""
ClipIt - Video Copyright Scanner & Clipper
Upload a video, scan for copyrighted music, clip segments with audio removed.
"""

import os
import json
import tempfile
import subprocess
import shutil
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)

# Configuration
AUDD_API_TOKEN = os.environ.get('AUDD_API_TOKEN', '')

# Chunk configuration for audio analysis
CHUNK_DURATION = 12  # seconds per chunk
OVERLAP = 4  # seconds overlap between chunks
MERGE_GAP = 30  # seconds gap tolerance for merging same-song detections


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


def analyze_audio(audio_path: str, max_duration: float = None) -> dict:
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
        
        # Analyze each chunk
        detected_songs = {}
        
        for i, (start_time, chunk_duration) in enumerate(chunks):
            chunk_path = os.path.join(temp_dir, f'chunk_{i}.mp3')
            
            try:
                extract_audio_chunk(audio_path, start_time, chunk_duration, chunk_path)
                
                result = recognize_with_audd(chunk_path)
                parsed = parse_audd_result(result)
                
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


@app.route('/')
def index():
    return jsonify({'status': 'ClipIt API is running', 'version': '1.0'})


@app.route('/api/scan', methods=['POST'])
def scan():
    """Scan uploaded video for copyrighted music."""
    
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
        
        # Get video duration
        duration = get_media_duration(video_path)
        
        # Extract audio
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
        
        # Get max_duration parameter
        max_duration = request.form.get('max_duration', None)
        if max_duration:
            max_duration = float(max_duration)
        
        # Analyze audio
        results = analyze_audio(audio_path, max_duration=max_duration)
        results['filename'] = file.filename
        results['video_duration'] = duration
        results['video_duration_formatted'] = format_timestamp(duration)
        
        return jsonify(results)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
        
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


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
    return jsonify({
        'status': 'ready',
        'audd_configured': bool(AUDD_API_TOKEN),
        'max_file_size': '100MB',
        'supported_formats': ['mp4', 'mov', 'avi', 'mkv', 'webm', 'm4v', 'wmv', 'flv']
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
