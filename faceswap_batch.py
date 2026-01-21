import os
import subprocess

IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.bmp', '.gif')
VIDEO_EXTENSIONS = ('.mp4', '.mov', '.mkv', '.avi')


def find_images(directory):
    """Returns a list of image paths found in the given directory."""
    return [
        os.path.join(directory, file)
        for file in os.listdir(directory)
        if file.lower().endswith(IMAGE_EXTENSIONS)
    ]


def find_videos(directory):
    """Returns a list of video paths found in the given directory."""
    return [
        os.path.join(directory, file)
        for file in os.listdir(directory)
        if file.lower().endswith(VIDEO_EXTENSIONS)
    ]


def process_videos(source_dir, target_dir, output_dir):
    source_paths = find_images(source_dir)
    if not source_paths:
        print("No source images found.")
        return

    if len(source_paths) > 1:
        print("More than one source image found. Using the first image.")

    source_path = source_paths[0]
    print(f"Using source image: {os.path.basename(source_path)}")

    target_paths = find_videos(target_dir)
    if not target_paths:
        print("No target videos found.")
        return

    os.makedirs(output_dir, exist_ok=True)

    for target_path in target_paths:
        target_filename = os.path.splitext(os.path.basename(target_path))[0]
        source_filename = os.path.splitext(os.path.basename(source_path))[0]
        output_filename = f"{source_filename}_{target_filename}.mp4"
        output_path = os.path.join(output_dir, output_filename)

        command = [
            "python",
            "run.py",
            "--headless",
            "--reference-face-distance",
            "1.2",
            "--skip-download",
            "--output-image-quality",
            "100",
            "--output-video-fps",
            "15",
            "-s",
            source_path,
            "-t",
            target_path,
            "-o",
            output_path,
        ]

        print(
            "Starting to process "
            f"{os.path.basename(target_path)} using source {os.path.basename(source_path)}..."
        )
        result = subprocess.run(" ".join(command), shell=True, check=False)
        if result.returncode != 0:
            print(
                "Failed to process with source "
                f"{os.path.basename(source_path)} and target {os.path.basename(target_path)} "
                f"with return code {result.returncode}"
            )
        else:
            print(f"Successfully processed to {output_filename}")
        print(
            "Completed processing "
            f"{os.path.basename(target_path)}. Moving to next target...\n"
        )

    print("All target videos have been processed.")


# Define the directories
source_directory = r"C:\DATEN\SS\_Source\Single"
target_directory = r"C:\DATEN\SS\_Target_Mult_Video"
output_directory = r"C:\DATEN\SS\_Output"

# Process all target videos using the first found image as the source
process_videos(source_directory, target_directory, output_directory)
