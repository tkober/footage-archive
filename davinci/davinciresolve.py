import json
import re
from enum import Enum
from pathlib import Path

import pandas as pd


class MetadataColumns(str, Enum):
    FILE_NAME = "File Name"
    CLIP_DIRECTORY = "Clip Directory"
    DURATION_TC = "Duration TC"
    SHOT_FRAME_RATE = "Shot Frame Rate"
    AUDIO_SAMPLE_RATE = "Audio Sample Rate"
    AUDIO_CHANNELS = "Audio Channels"
    RESOLUTION = "Resolution"
    VIDEO_CODEC = "Video Codec"
    AUDIO_CODEC = "Audio Codec"
    DESCRIPTION = "Description"
    KEYWORDS = "Keywords"
    SHOT = "Shot"
    SCENE = "Scene"
    TAKE = "Take"
    ANGLE = "Angle"
    MOVE = "Move"
    SHOT_TYPE = "Shot Type"
    DATE_RECORDED = "Date Recorded"
    BIT_DEPTH = "Bit Depth"
    AUDIO_BIT_DEPTH = "Audio Bit Depth"
    DATE_MODIFIED = "Date Modified"


class DerivedMetadataColumns(str, Enum):
    JSON = 'json'
    WIDTH = 'width'
    HEIGHT = 'height'
    FRAME_RATE = 'frame_rate'
    FILE_PATH = 'file_path'
    KEYWORD = 'keyword'


class ResolutionFactor(int, Enum):
    WIDTH = 0
    HEIGHT = 1


class Metadata:
    _csv: pd.DataFrame

    def __init__(self, csv_file: Path):
        self._csv = pd.read_csv(csv_file, delimiter=',', encoding='utf-16')

    def __get_resolution_factor(self, row: pd.Series, factor: ResolutionFactor) -> pd.Series:
        return row[MetadataColumns.RESOLUTION.value].split('x')[factor.value]

    def __get_raw_frame_rate(self, row: pd.Series) -> pd.Series:
        return re.match(r'([0-9]+[.][0-9]*)(.*)', row[MetadataColumns.SHOT_FRAME_RATE.value]).group(1)

    def get_details(self) -> pd.DataFrame:
        df = self._csv.copy(deep=True)

        # Extract width and height
        df[DerivedMetadataColumns.WIDTH.value] = df.apply(lambda row: self.__get_resolution_factor(row, ResolutionFactor.WIDTH), axis=1)
        df[DerivedMetadataColumns.HEIGHT.value] = df.apply(lambda row: self.__get_resolution_factor(row, ResolutionFactor.HEIGHT), axis=1)

        # Extract raw frame rate
        df[DerivedMetadataColumns.FRAME_RATE.value] = df.apply(lambda row: self.__get_raw_frame_rate(row), axis=1)

        # Save the entire row as json
        df[DerivedMetadataColumns.JSON.value] = df.apply(lambda row: json.dumps(row.dropna().to_dict()), axis=1)

        # Set path
        df[DerivedMetadataColumns.FILE_PATH.value] = df.apply(
            lambda row: str(Path(row[MetadataColumns.CLIP_DIRECTORY.value]) / row[MetadataColumns.FILE_NAME.value]), axis=1)

        # Filter for essential ones
        cols = [
            col.value for col in MetadataColumns
            if col not in [
                MetadataColumns.KEYWORDS,
                MetadataColumns.FILE_NAME,
                MetadataColumns.CLIP_DIRECTORY
            ]
        ]
        cols += [col.value for col in DerivedMetadataColumns if col not in [DerivedMetadataColumns.KEYWORD]]
        df = df[cols]

        # Simple renames
        df = df.rename(columns={
            MetadataColumns.DURATION_TC: 'duration_tc',
            MetadataColumns.SHOT_FRAME_RATE: 'frame_rate_verbose',
            MetadataColumns.AUDIO_SAMPLE_RATE: 'audio_sample_rate',
            MetadataColumns.AUDIO_CHANNELS: 'audio_channels',
            MetadataColumns.RESOLUTION: 'resolution',
            MetadataColumns.VIDEO_CODEC: 'video_codec',
            MetadataColumns.AUDIO_CODEC: 'audio_codec',
            MetadataColumns.DESCRIPTION: 'description',
            MetadataColumns.SHOT: 'shot',
            MetadataColumns.SCENE: 'scene',
            MetadataColumns.TAKE: 'take',
            MetadataColumns.ANGLE: 'angle',
            MetadataColumns.MOVE: 'move',
            MetadataColumns.SHOT_TYPE: 'shot_type',
            MetadataColumns.DATE_RECORDED: 'recorded_at',
            MetadataColumns.BIT_DEPTH: 'bit_depth',
            MetadataColumns.AUDIO_BIT_DEPTH: 'audio_bit_depth',
            MetadataColumns.DATE_MODIFIED: 'last_modified_at',
        })

        return df

    def get_keywords(self) -> pd.DataFrame:
        df = self._csv.copy(deep=True)

        # Set path
        df[DerivedMetadataColumns.FILE_PATH.value] = self._csv.apply(
            lambda row: str(Path(row[MetadataColumns.CLIP_DIRECTORY.value]) / row[MetadataColumns.FILE_NAME.value]), axis=1)

        df  = df[[DerivedMetadataColumns.FILE_PATH.value, MetadataColumns.KEYWORDS.value]]
        df[MetadataColumns.KEYWORDS.value] = df[MetadataColumns.KEYWORDS.value].str.split(',')
        df = df.explode(MetadataColumns.KEYWORDS.value)

        df = df.rename(columns={MetadataColumns.KEYWORDS.value : DerivedMetadataColumns.KEYWORD.value})
        df = df.dropna(subset=[DerivedMetadataColumns.KEYWORD.value])
        df[DerivedMetadataColumns.KEYWORD.value] = df.apply(lambda row: str(row['keyword']).lower().strip(), axis=1)
        df = df.drop_duplicates()

        return df


if __name__ == '__main__':
    kw = Metadata(Path('/Users/kober/Desktop/Kokura_Story Media Metadata.csv')).get_keywords()
    print(kw)
