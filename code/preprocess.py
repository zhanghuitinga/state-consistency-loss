import os
import tqdm
import h5py
import rosbag
import numpy as np
import pandas as pd
from datetime import datetime
from next import compute_occupancy_maps
from utils import jpeg2np, quaternion2yaw
from settings import input_cols, target_cols, coords


def preprocess(path, extractors, tolerance='1s',
               input_cols=None, target_cols=None,
               coords=[(0, 0)], interval='15s',
               delta=0.1, fillna=None):
    '''Extracts data from bagfiles, finds readings for poses at a given relative coordinate displacement,
            and saves everything into an HDF5 file.

    Args:
            path: the path in which bag files are stored.
            extractors: a dictionary of functions associated to ROS topics that extracts the required values,
                                    composed of ROS topics as keys and functions as values.
            tolerance: a string representing the time tolerance used to merge the different ROS topics.
            target_cols: a list of column names used as source of sensors' readings.
            coords: a list of relative coordinates of the form [(x1, y1), ...].
            interval: a time interval, expressed as string, for limiting the search along time of relative displaced poses.
            delta: the maximum distance of a candidate pose to a relative coordinate displacement for being accepted.
            fillna: a value used as a replacement for missing data.
    '''
    files = [file[:-4] for file in os.listdir(path) if file[-4:] == '.bag']

    if not files:
        print('no bag files found in "' + path + '"')
        return

    h5f = h5py.File('dataset_' + str(datetime.now()) + '.h5', 'w')

    # for each bagfile
    for index, file in enumerate(sorted(files)):
        filename = path + file + '.bag'

        print('found ' + filename)

        # extract one dataframe per topic
        dfs = bag2dfs(rosbag.Bag(filename), extractors)

        # merge the dataframes based on the timeindex
        df = mergedfs(dfs, tolerance=tolerance)

        # note: fictitiuos world_id for real data
        # df['world_id'] = index

        if target_cols is None:
            target_cols = [col for col in df.columns.values if 'target' in col]

        if input_cols is None:
            input_cols = [col for col in df.columns.values
                          if col not in target_cols]

        print('extracting occupancy maps...')

        df, output_cols = compute_occupancy_maps(df, coords, target_cols,
                                                 interval=interval, delta=delta)

        if fillna is not None:
            df.fillna(fillna, inplace=True)

        print('saving...')

        for wid, world_df in df.groupby('world_id'):
            wid = str(wid)
            length = len(world_df)
            for col in input_cols + output_cols:
                name = 'bag' + str(index) + '/world' + wid + '/' + col
                shape = np.array(world_df[col].iloc[0]).shape
                store = h5f.create_dataset(name,
                                           shape=(length,) + shape,
                                           maxshape=(None,) + shape,
                                           dtype=np.float, chunks=True,
                                           data=None)
                store[:] = np.stack(world_df[col].values)

    h5f.close()


def bag2dfs(bag, extractors):
    '''Extracts data from a ROS bagfile and converts it to dataframes (one per topic).

    Args:
            bag: a ROS bagfile.
            extractors: a dictionary of functions associated to ros topics that extracts the required values,
                                    composed of ros topics as keys and functions as values.
    Returns:
            a dictionary of dataframes divided by ROS topic.
    '''
    result = {}

    for topic in tqdm.tqdm(extractors.keys(), desc='extracting data from the bagfile'):
        timestamps = []
        values = []

        for subtopic, msg, t in bag.read_messages(topic):
            if subtopic == topic:
                # if msg._has_header:
                #     timestamps.append(msg.header.stamp.to_nsec())
                timestamps.append(t.to_nsec())
                values.append(extractors[topic](msg))

        if not values:
            raise ValueError('Topic "' + topic +
                             '" not found in one of the bagfiles')

        df = pd.DataFrame(data=values, index=timestamps,
                          columns=values[0].keys())

        # note: avoid duplicated timestamps generated by ros
        df = df[~df.index.duplicated()]

        result[topic] = df

    return result


def get_odom(m, robot_name):
    '''Extracts odometry information from /gazebo/model_states messages.'''
    result = {}

    index = m.name.index(robot_name)
    pose = m.pose[index]
    result['pos_x'] = pose.position.x
    result['pos_y'] = pose.position.y
    result['theta'] = quaternion2yaw(pose.orientation)

    index = m.name.index('ooi')
    pose = m.pose[index]
    result['ooi_pos_x'] = pose.position.x
    result['ooi_pos_y'] = pose.position.y
    result['ooi_theta'] = quaternion2yaw(pose.orientation)

    return result


def mergedfs(dfs, tolerance='1s'):
    '''Merges different dataframes indexed by datetime into a synchronized dataframe.

    Args:
            dfs: a dictionary of dataframes.
            tolerance: a string representing the time tolerance used to merge the different dataframes.

    Returns:
            a single dataframe composed of the various dataframes synchronized.
    '''
    min_topic = None

    # find topic with fewest datapoints
    for topic, df in dfs.items():
        if not min_topic or len(dfs[min_topic]) > len(df):
            min_topic = topic

    ref_df = dfs[min_topic]
    other_dfs = dfs
    other_dfs.pop(min_topic)

    # merge dfs with a time tolerance
    result = pd.concat(
        [ref_df] +
        [df.reindex(index=ref_df.index, method='nearest', tolerance=pd.Timedelta(
            tolerance).value) for _, df in other_dfs.items()],
        axis=1)

    result.dropna(inplace=True)
    result.index = pd.to_datetime(result.index)

    return result


if __name__ == '__main__':
    prefix = '/thymio10'

    path = './data/bag/'

    extractors = {
        prefix + '/camera/image_raw/compressed': lambda m: {
            'camera': jpeg2np(m.data, (80, 64), normalize=True)
        },

        prefix + '/proximity/center': lambda m: {
            'target_center_sensor': m.range
        },
        prefix + '/proximity/center_left': lambda m: {
            'target_center_left_sensor': m.range
        },
        prefix + '/proximity/center_right': lambda m: {
            'target_center_right_sensor': m.range
        },
        prefix + '/proximity/left': lambda m: {
            'target_left_sensor': m.range
        },
        prefix + '/proximity/right': lambda m: {
            'target_right_sensor': m.range
        },

        # note: this is used for the simulated thymio
        '/model_states': lambda m: get_odom(m, prefix.replace('/', '')),
        '/world/id': lambda m: {
            'world_id': m.data
        }

        # note: this is used for the real thymio
        # prefix + '/odom': lambda m: {
        #     'pos_x': m.pose.pose.position.x,
        #     'pos_y': m.pose.pose.position.y,
        #     'theta': quaternion2yaw(m.pose.pose.orientation)
        # }
    }

    res = preprocess(path=path, extractors=extractors, tolerance='0.5s',
                     input_cols=input_cols, target_cols=target_cols,
                     coords=coords, interval='10s', delta=0.04, fillna=-1.0)