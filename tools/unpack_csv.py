"""Extract an archived CSV alongside its original .csv.xz file."""

import argparse
import lzma
import shutil
from contextlib import suppress
from pathlib import Path


def unpack_csv(archive):
    output = archive.with_suffix('')
    if archive.suffix.lower() != '.xz' or output.suffix.lower() != '.csv':
        raise ValueError('.csv.xz 파일을 선택해 주세요.')
    with lzma.open(archive, 'rb') as source:
        destination = output.open('xb')
        try:
            with destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
        except BaseException:
            with suppress(OSError):
                output.unlink()
            raise
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='압축한 13F CSV의 원본을 복원합니다.')
    parser.add_argument('archive', type=Path, help='.csv.xz 파일 경로')
    args = parser.parse_args()
    try:
        path = unpack_csv(args.archive)
    except FileExistsError:
        parser.error('같은 이름의 CSV가 이미 있습니다. 기존 파일의 이름이나 위치를 바꾼 뒤 다시 실행해 주세요.')
    except (OSError, ValueError, EOFError, lzma.LZMAError) as exc:
        parser.error(str(exc))
    print(path.resolve())
