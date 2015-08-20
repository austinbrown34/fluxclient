# !/usr/bin/env python3
import struct
import io
import subprocess
import tempfile
import os
import sys

try:
    import fluxclient.printer._printer as _printer
except:
    pass
from fluxclient.fcode.g_to_f import GcodeToFcode


class StlSlicer(object):
    """slicing objects"""
    def __init__(self):
        super(StlSlicer, self).__init__()
        self.reset()

    def reset(self):
        self.models = {}  # models data
        self.parameter = {}  # model's parameter
        self.user_setting = {}  # slcing setting
        self.slic3r = '../Slic3r/slic3r.pl'
        self.slic3r_setting = './fluxghost/assets/flux_slicing.ini'

    def upload(self, name, buf):
        self.models[name] = buf

    def delete(self, name):
        if name in self.models:
            del self.models[name]
            if name in self.parameter:
                del self.parameter[name]
        else:
            raise ValueError("%s not upload yet" % (name))

    def set(self, name, parameter):
        if name in self.models:
            self.parameter[name] = parameter
        else:
            raise ValueError("%s not upload yet" % (name))

    def set_params(self, key, value):
        if key in ['printSpeed', 'material', 'raft', 'support', 'layerHeight', 'infill', 'travelingSpeed', 'extrudingSpeed', 'temperature']:
            self.user_setting[key] = value
            return True
        else:
            return False

    def generate_gcode(self, names):
        ## psudo code
        ## self.mesh = Mesh(pcl mesh)
        m_mesh_merge = _printer.MeshObj([], [])
        for n in names:
            points, faces = self.read_stl(self.models[n])
            m_mesh = _printer.MeshObj(points, faces)
            m_mesh.apply_transform(self.parameter[n])
            m_mesh_merge.add_on(m_mesh)

        bounding_box = m_mesh_merge.bounding_box()
        cx, cy = (bounding_box[0][0] + bounding_box[1][0]) / 2., (bounding_box[0][1] + bounding_box[1][1]) / 2.

        tmp = tempfile.NamedTemporaryFile(suffix='.stl', delete=False)
        tmp_stl_file = tmp.name  # store merged stl

        m_mesh_merge.write_stl(tmp_stl_file)

        tmp = tempfile.NamedTemporaryFile(suffix='.gcode', delete=False)
        tmp_gcode_file = tmp.name  # store gcode

        tmp = tempfile.NamedTemporaryFile(suffix='.ini', delete=False)
        tmp_slic3r_setting_file = tmp.name  # store gcode

        command = [self.slic3r, tmp_stl_file]

        command += ['--output', tmp_gcode_file]
        command += ['--print-center', '%f,%f' % (cx, cy)]

        config = self.my_ini_parser(self.slic3r_setting)
        config['gcode_comments'] = '1'
        for key in self.user_setting:
            if self.user_setting[key] != "default":
                if key == 'printSpeed':
                    pass  # TODO
                elif key == 'material':
                    pass
                elif key == 'raft':
                    if self.user_setting[key] == '0':
                        config['raft_layers'] = '0'

                    # TODO
                    # elif self.user_setting[key] == '1':
                    #     config['raft_layers'] =
                elif key == 'support':
                    config['support_material'] = self.user_setting[key]
                elif key == 'layerHeight':
                    config['first_layer_height'] = self.user_setting[key]
                    config['layer_height'] = self.user_setting[key]
                elif key == 'infill':
                    fill_density = float(self.user_setting[key]) * 100
                    config['fill_density'] = str(fill_density)
                elif key == 'travelingSpeed':
                    config['travel_speed'] = self.user_setting[key]
                elif key == 'extrudingSpeed':
                    config['perimeter_speed'] = self.user_setting[key]
                    config['infill_speed'] = self.user_setting[key]
                elif key == 'temperature':
                    config['temperature'] = self.user_setting[key]

        self.my_ini_writer(tmp_slic3r_setting_file, config)

        command += ['--load', tmp_slic3r_setting_file]
        print('command:', ' '.join(command), file=sys.stderr)
        slic3r_out = subprocess.check_output(command)
        slic3r_out = slic3r_out.decode('utf8')
        print(slic3r_out, file=sys.stderr)

        with open(tmp_gcode_file, 'r') as f:
            gcode = f.read()
        with open(tmp_gcode_file, 'r') as f:
            m_GcodeToFcode = GcodeToFcode()
            m_GcodeToFcode.process(f, io.BytesIO())
            metadata = m_GcodeToFcode.md
            metadata = [float(metadata['TIME_COST']), float(metadata['FILAMENT_USED'])]

        ##################### fake code ###########################
        with open('output.gcode', 'w') as f:
            print(gcode, file=f, end='')

        with open(tmp_stl_file, 'rb') as f:
            with open('merged.stl', 'wb') as f2:
                f2.write(f.read())
        ###########################################################

        # clean up tmp files
        os.remove(tmp_stl_file)
        os.remove(tmp_gcode_file)
        os.remove(tmp_slic3r_setting_file)

        return gcode, metadata

    @classmethod
    def my_ini_parser(cls, file_path):
        result = {}
        with open(file_path, 'r') as f:
            for i in f.readlines():
                if i[0] == '#':
                    pass
                elif '=' in i:
                    tmp = i.rstrip().split('=')
                    result[tmp[0].rstrip()] = tmp[1].rstrip()
                else:
                    print(i)
                    raise ValueError('not ini file?')
        return result

    @classmethod
    def my_ini_writer(cls, file_path, content):
        with open(file_path, 'w') as f:
            for i in content:
                print("%s=%s" % (i, content[i]), file=f)
        return

    @classmethod
    def ascii_or_binary(cls, data, byte_order):
        """
        check what kind of stl file it is
        return False -> binary
        return True -> ascii
        """
        if not data.startswith(b'solid '):
            return False

        length = struct.unpack(byte_order + 'I', data[80:84])[0]
        if len(data) == 80 + 4 + length * 50:
            return False
        return True

    @classmethod
    def read_stl(cls, file_data):
        # https://en.wikipedia.org/wiki/STL_(file_format)
        if type(file_data) == str:
            with open(file_data, 'rb') as f:
                file_data = f.read()
                byte_order = '@'
        elif type(file_data) == bytes:
            byte_order = '<'
        else:
            raise ValueError('wrong stl data type:%s' % str(type(file_data)))
        points = {}  # key: points, value: index
        faces = []
        counter = 0
        if cls.ascii_or_binary(file_data, byte_order):
            # ascii stl file
            instl = io.StringIO(file_data.decode('utf8'))
            instl.readline()  # read in: "solid [name]"

            while True:
                t = instl.readline()  # read in: "facet normal 0 0 0"
                if t[:8] != 'endsolid':  # end of file
                    instl.readline()   # outer loop
                    v0 = tuple(map(float, (instl.readline().split()[-3:])))
                    v1 = tuple(map(float, (instl.readline().split()[-3:])))
                    v2 = tuple(map(float, (instl.readline().split()[-3:])))

                    instl.readline()  # read in: "endloop"
                    instl.readline()  # read in: "endfacet"

                else:
                    break
                face = []
                for v in [v0, v1, v2]:
                    if v not in points:
                        points[v] = counter
                        points[counter] = v
                        counter += 1
                    face.append(points[v])
                faces.append(face)

        else:
            # binary stl file
            header = file_data[:80]
            length = struct.unpack(byte_order + 'I', file_data[80:84])[0]

            for i in range(length):
                index = i * 50 + 84
                v0 = struct.unpack(byte_order + 'fff', file_data[index + (4 * 3 * 1):index + (4 * 3 * 2)])
                v1 = struct.unpack(byte_order + 'fff', file_data[index + (4 * 3 * 2):index + (4 * 3 * 3)])
                v2 = struct.unpack(byte_order + 'fff', file_data[index + (4 * 3 * 3):index + (4 * 3 * 4)])
                face = []
                for v in [v0, v1, v2]:
                    if v not in points:
                        points[v] = counter
                        points[counter] = v
                        counter += 1
                    face.append(points[v])
                faces.append(face)

        points_list = []
        for i in range(counter):
            points_list.append(points[i])
        return points_list, faces


class StlSlicerNoPCL(StlSlicer):
    """docstring for StlSlicerNoPCL"""
    def __init__(self):
        super(StlSlicerNoPCL, self).__init__()
        self.reset()

    def generate_gcode(self, names):
        ############### fake code ###############
        gcode = "Nothing to do here~~"
        metadata = [1000., 300.0]
        ############### fake code ###############
        return gcode, metadata
