"""Create CPU contention for a live demo, so the governor can be seen engaging.

    python scripts/demo_load.py 60     # one busy process per logical core for 60 s
"""
import multiprocessing as mp
import sys
import time


def hog(seconds: float) -> None:
    # Each worker has its own deadline, so none can outlive the demo if the parent is killed.
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        sum(i * i for i in range(10_000))


if __name__ == "__main__":
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 60
    workers = [mp.Process(target=hog, args=(seconds,), daemon=True) for _ in range(mp.cpu_count())]
    for worker in workers:
        worker.start()
    print(f"{len(workers)} busy processes running for {seconds:.0f} s")
    time.sleep(seconds)
    for worker in workers:
        worker.terminate()
