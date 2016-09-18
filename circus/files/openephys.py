import h5py, numpy, re, sys
import ConfigParser as configparser
from circus.shared.messages import print_error, print_and_log
from datafile import DataFile

class OpenEphysFile(DataFile):

    _description = "openephys"    
    _extension   = [".continuous"]
    _parallel_write = False

    def __init__(self, file_name, params, empty=False, comm=None):

        DataFile.__init__(self, file_name, params, empty, comm)
        if not self.empty:
            self._get_info_()

    def _get_info_(self, key=None):
        pass

    def allocate(self, shape, data_dtype=None):
        pass

    def get_data(self, idx, chunk_size=None, padding=(0, 0), nodes=None):
        pass

    def set_data(self, time, data):
        pass

    def open(self, mode='r'):
        pass
        
    def close(self):
        pass
