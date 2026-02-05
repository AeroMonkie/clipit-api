"""
ClipIt - Video Segment Extractor
Upload a video, specify timestamps, get the trimmed clip.
"""

import os
import tempfile
import subprocess
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# Max file size (100MB)
MAX_FILE_SIZE = 100 * 1024 * 1024


def parse_timestamp(ts: str) -> float:
    """Convert timestamp string (MM:SS or HH:MM:SS) to seconds."""
    ts = ts.strip()
    parts = ts.split(':')
    
    if len(parts) == 2:
        # MM:SS
        minutes, seconds = parts
        return int(minutes) * 60 + float(seconds)
    elif len(parts) == 3:
        # HH:MM:SS
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    else:
        raise ValueError(f"Invalid timestamp format: {ts}")


def get_video_duration(video_path: str) -> float:
    """Get duration of video file using ffprobe."""
    cmd = [
        'ffprobe',
        '-v', 'quiet',
        '-show_entries', 'format=duration',
        '-of', 'csv=p=0',
        video_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"ffprobe failed: {result.stderr}")
    
    return float(result.stdout.strip())


def clip_video(input_path: str, output_path: str, start_time: float, end_time: float):
    """Extract a segment from video using ffmpeg."""
    duration = end_time - start_time
    
    cmd = [
        'ffmpeg',
        '-y',
        '-ss', str(start_time),
        '-i', input_path,
        '-t', str(duration),
        '-c:v', 'libx264',
        '-c:a', 'aac',
        '-preset', 'fast',
        '-crf', '23',
        '-movflags', '+faststart',
        output_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise Exception(f"ffmpeg failed: {result.stderr}")


def format_timestamp(seconds: float) -> str:
    """Format seconds to MM:SS or HH:MM:SS."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


@app.route('/')
def index():
    return jsonify({'status': 'ClipIt API is running', 'version': '1.0'})


@app.route('/api/clip', methods=['POST'])
def clip():
    """API endpoint to clip a video segment."""
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    start_ts = request.form.get('start', '')
    end_ts = request.form.get('end', '')
    
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    if not start_ts or not end_ts:
        return jsonify({'error': 'Start and end timestamps are required'}), 400
    
    # Check file extension
    allowed_extensions = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v', '.wmv', '.flv'}
    file_ext = os.path.splitext(file.filename)[1].lower()
    
    if file_ext not in allowed_extensions:
        return jsonify({'error': f'Invalid file type. Allowed: {", ".join(allowed_extensions)}'}), 400
    
    # Parse timestamps
    try:
        start_seconds = parse_timestamp(start_ts)
        end_seconds = parse_timestamp(end_ts)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    
    if start_seconds >= end_seconds:
        return jsonify({'error': 'Start time must be before end time'}), 400
    
    if start_seconds < 0:
        return jsonify({'error': 'Start time cannot be negative'}), 400
    
    # Create temp directory
    temp_dir = tempfile.mkdtemp()
    input_path = os.path.join(temp_dir, f'input{file_ext}')
    output_path = os.path.join(temp_dir, 'output.mp4')
    
    try:
        # Save uploaded file
        file.save(input_path)
        
        # Get video duration
        duration = get_video_duration(input_path)
        
        if end_seconds > duration:
            return jsonify({'error': f'End time ({end_ts}) exceeds video duration ({format_timestamp(duration)})'}), 400
        
        # Clip the video
        clip_video(input_path, output_path, start_seconds, end_seconds)
        
        # Generate output filename
        original_name = os.path.splitext(file.filename)[0]
        output_filename = f"{original_name}_clip_{start_ts.replace(':', '-')}_to_{end_ts.replace(':', '-')}.mp4"
        
        # Send the clipped file
        return send_file(
            output_path,
            as_attachment=True,
            download_name=output_filename,
            mimetype='video/mp4'
        )
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
        
    finally:
        # Clean up input file (output cleaned up after send)
        if os.path.exists(input_path):
            os.remove(input_path)


@app.route('/api/config', methods=['GET'])
def get_config():
    """Check API status."""
    return jsonify({
        'status': 'ready',
        'max_file_size': '100MB',
        'supported_formats': ['mp4', 'mov', 'avi', 'mkv', 'webm', 'm4v', 'wmv', 'flv'],
        'output_format': 'mp4'
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
