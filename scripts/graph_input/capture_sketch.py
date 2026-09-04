"""
capture_sketch.py

Webcam capture utility for feeding hand-drawn graph sketches into the
image-to-JSON extraction pipeline (image_to_json.py).

Usage:
    python capture_sketch.py
    python capture_sketch.py --output-dir images --camera 0

Controls (while the preview window is focused):
    SPACE  -> capture current frame and save it
    ESC    -> quit
    q      -> also quits

Each captured image is saved as:
    <output_dir>/sketch_YYYYMMDD_HHMMSS.png

Requires: opencv-python
    pip install opencv-python --break-system-packages
"""

import argparse
import os
import sys
from datetime import datetime

import cv2


def parse_args():
    parser = argparse.ArgumentParser(description="Capture hand-drawn graph sketches via webcam.")
    parser.add_argument("--output-dir", default="images", help="Directory to save captured images into (default: ./images)")
    parser.add_argument("--camera", type=int, default=0, help="Camera index to use (default: 0)")
    parser.add_argument("--prefix", default="sketch", help="Filename prefix (default: 'sketch')")
    return parser.parse_args()


def main():
    args = parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        print(f"Error: could not open camera index {args.camera}.", file=sys.stderr)
        sys.exit(1)

    window_name = "Sketch Capture - SPACE to capture, ESC to quit"
    print("Webcam preview starting...")
    print("  SPACE -> capture and save frame")
    print("  ESC / q -> quit")

    captured_paths = []

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("Warning: failed to read frame from camera.", file=sys.stderr)
                break

            preview = frame.copy()
            cv2.putText(preview, "SPACE: capture | ESC: quit", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            cv2.imshow(window_name, preview)

            key = cv2.waitKey(1) & 0xFF

            if key == 27 or key == ord("q"):
                break
            elif key == 32:  # SPACE
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"{args.prefix}_{timestamp}.png"
                filepath = os.path.join(args.output_dir, filename)
                cv2.imwrite(filepath, frame)
                captured_paths.append(filepath)
                print(f"Captured: {filepath}")

    finally:
        cap.release()
        cv2.destroyAllWindows()

    if captured_paths:
        print(f"\nDone. {len(captured_paths)} image(s) saved to '{args.output_dir}':")
        for p in captured_paths:
            print(f"  - {p}")
    else:
        print("\nNo images captured.")

    return captured_paths


if __name__ == "__main__":
    main()