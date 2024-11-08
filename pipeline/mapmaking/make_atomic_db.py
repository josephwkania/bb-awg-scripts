import h5py
import numpy as np
import sqlite3
from glob import glob
import os

def make_db_from_outdir(odir, atomic_db_path):
    """Make an atomic db from all atomics in a directory and its subdirectories"""
    file_list = glob(os.path.join(odir, '**/*info.hdf'), recursive=True)
    make_db(file_list, atomic_db_path)
    return read_db(atomic_db_path)
    
def make_db(info_file_list, atomic_db_path):
    if len(info_file_list) == 0:
        raise RuntimeError("Cannot make db from empty file list")
    conn = sqlite3.connect(atomic_db_path)
    cursor = conn.cursor()
    
    entries = [('obs_id', 'TEXT'), ('telescope', 'TEXT'), ('freq_channel', 'TEXT'), ('wafer', 'TEXT'), ('ctime', 'INTEGER'), ('split_label', 'TEXT'),
               ('split_detail', 'TEXT'), ('prefix_path', 'TEXT'), ('elevation', 'REAL'), ('azimuth', 'REAL'), ('RA_ref_start', 'REAL'),
               ('RA_ref_stop', 'REAL'), ('pwv', 'REAL'), ('total_weight_qu', 'REAL'), ('median_weight_qu', 'REAL'), ('mean_weight_qu', 'REAL')]

    cmd = f"CREATE TABLE IF NOT EXISTS atomic ({', '.join([' '.join(tup) for tup in entries])})"
    cursor.execute(cmd)
    
    conn.commit()
    for info_file in info_file_list:
        info = parse_info(info_file)
        info_tuple = info_dict_to_tuple(info, entries)

        cursor.execute(f"INSERT INTO atomic VALUES ({', '.join(['?']*len(info_tuple))})", info_tuple)

    conn.commit()    
    conn.close()

def read_db(db_filename, *args):
    """Read from a db. 
    String args can be passed to make a query eg 'obs_id="xxx"'
    """
    conn = sqlite3.connect(db_filename)
    cursor = conn.cursor()
    query = 'SELECT * from atomic'
    if len(args) > 0:
        query += ' where ' + " and ".join(args)
    res = cursor.execute(query)
    matches = res.fetchall()
    return matches
    
def load_h5(fn):
    if isinstance(fn, h5py._hl.files.File):
        return fn
    elif isinstance(fn, str):
        return h5py.File(fn, 'r')
    else:
        raise TypeError(f"Invalid type for hdf5 file(name) {type(fn)}")

def parse_info(h5):
    h5 = load_h5(h5)
    out = {}
    for key in h5.keys():
        item = np.squeeze(np.asarray(h5[key]))
        if item.ndim == 0:
            item = item[()] # Extract scalars
        if isinstance(item, bytes):
            item = item.decode('UTF-8') # Convert bytes to strings
        out[key] = item
    return out

def info_dict_to_tuple(dct, entry_list):
    out = []
    dtype_dict = {'TEXT':str, 'INTEGER':int, 'REAL':float}
    
    for entry in entry_list:
        dtype = dtype_dict[entry[1]]
        out.append(dtype(dct[entry[0]]))
    return tuple(out)

def main():
    import sys
    odir, atomic_db_path = sys.argv[1:3]
    make_db_from_outdir(odir, atomic_db_path)
    
if __name__ == '__main__':
    main()
