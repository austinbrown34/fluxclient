# !/usr/bin/env python3

from struct import unpack
from io import BytesIO, StringIO
import subprocess
import tempfile
import os
from os import remove, environ
import platform
import sys
from multiprocessing import Pipe
from threading import Thread
import logging

from PIL import Image
# import pkg_resources

from fluxclient.hw_profile import HW_PROFILE
from fluxclient.printer import _printer
from fluxclient.fcode.g_to_f import GcodeToFcode
from fluxclient.scanner.tools import dot, normal, normalize
from fluxclient.printer import ini_string, ini_constraint, ignore
from fluxclient.printer.flux_raft import Raft

logger = logging.getLogger("printer.stl_slicer")


class StlSlicer(object):
    """slicing objects"""
    def __init__(self, slic3r):
        super(StlSlicer, self).__init__()
        self.reset(slic3r)

    def __del__(self):
        self.end_slicing()

    def reset(self, slic3r):
        self.working_p = []  # process that are slicing
        self.models = {}  # models data, store the buf(stl file)
        self.parameter = {}  # model's parameter
        self.user_setting = {}  # slcing setting

        # self.slic3r = '../Slic3r/slic3r.pl'  # slic3r's location
        # self.slic3r = '/Applications/Slic3r.app/Contents/MacOS/slic3r'
        self.slic3r = slic3r

        # self.slic3r_setting = './fluxghost/assets/flux_slicing.ini'
        self.config = self.my_ini_parser(ini_string.split('\n'))
        # self.config = self.my_ini_parser(self.slic3r_setting)
        self.config['gcode_comments'] = '1'  # force open comment in gcode generated
        self.path = None
        self.image = b''
        self.ext_metadata = {'CORRECTION': 'A'}

    def upload(self, name, buf):
        """
        upload a model's data in stl as bytes data
        """
        self.models[name] = buf

    def duplicate(self, name_in, name_out):
        """
        name_in[in]: name for the original one
        name_out[in] name for the new one

        duplicate a model in models(but not set position yet)
        """
        if name_in in self.models:
            self.models[name_out] = self.models[name_in]  # no need to copy, bytes is immutable
            return True
        else:
            return False

    def upload_image(self, buf):
        b = BytesIO()
        b.write(buf)
        img = Image.open(b)
        img = img.resize((640, 640))  # resize preview image

        b = BytesIO()
        img.save(b, 'png')
        image_bytes = b.getvalue()
        self.image = image_bytes
        ######################### fake code ###################################
        if environ.get("flux_debug") == '1':
            with open('preview.png', 'wb') as f:
                f.write(image_bytes)
        ############################################################

    def delete(self, name):
        """
        delete [name]
        """
        if name in self.models:
            del self.models[name]
            if name in self.parameter:
                del self.parameter[name]
            return True, 'OK'
        else:
            return False, "%s not upload yet" % (name)

    def set(self, name, parameter):
        """
        set the position, scale, rotation... parameters
        (just record it, didn't actually compute it)
        """
        if name in self.models:
            self.parameter[name] = parameter
        else:
            raise ValueError("%s not upload yet" % (name))

    def set_params(self, key, value):
        """
        basic printing parameter in front end
        """
        if key in ['print_speed', 'material', 'raft', 'support', 'layer_height', 'infill', 'traveling_speed', 'extruding_speed', 'temperature']:
            self.user_setting[key] = value
            return True
        else:
            return False

    def advanced_setting(self, lines):
        """
        user input  setting content
        use '#' as comment symbol (different from wiki's ini file standard)

        return error message of bad input

        """
        # TODO: close ignore when changing back
        counter = 1
        bad_lines = []
        for line in lines:
            if '#' in line:  # clean up comement
                line = line[:line.index('#')].strip()
            if '=' in line:
                key, value = map(lambda x: x.strip(), line.split('=', 1))
                result = self.ini_value_check(key, value)
                if result == 'ok':
                    self.config[key] = value
                    if key == 'temperature':
                        self.config['first_layer_temperature'] = str(min(230, float(value) + 5))
                    # elif key == 'overhangs' and value == '0':
                    #     self.config['support_material'] = '0'
                    #     ini_constraint['support_material'] = [ignore]
                    elif key == 'spiral_vase' and value == '1':
                        self.config['support_material'] = '0'
                        ini_constraint['support_material'] = [ignore]
                        self.config['fill_density'] = '0%'
                        ini_constraint['fill_density'] = [ignore]
                        self.config['perimeters'] = '1'
                        ini_constraint['perimeters'] = [ignore]
                        self.config['top_solid_layers'] = '0'
                        ini_constraint['top_solid_layers'] = [ignore]

                elif result == 'ignore':
                    # ignore this config key anyway
                    pass
                else:
                    bad_lines.append((counter, result))
            elif line != '' and line != 'default':
                bad_lines.append((counter, 'syntax error: %s' % line))
            counter += 1
        return bad_lines

    def get_path(self):
        """
        """
        if self.path:
            return GcodeToFcode.path_to_js(self.path)

    def gcode_generate(self, names, ws, output_type):
        """
        input: names of stl that need to be sliced
        output:
            if success:
                gcode (binary in bytes), metadata([TIME_COST, FILAMENT_USED])
            else:
                False, [error message]
        """

        # check if names are all seted
        for n in names:
            if not (n in self.models and n in self.parameter):
                return False, '%s not set yet' % (n)

        ws.send_progress('merging', 0.2)
        m_mesh_merge = _printer.MeshObj([], [])
        for n in names:
            try:
                points, faces = self.read_stl(self.models[n])
            except:
                return False, 'can\'t parse file, may not ba a stl file'
            m_mesh = _printer.MeshObj(points, faces)
            m_mesh.apply_transform(self.parameter[n])
            m_mesh_merge.add_on(m_mesh)
        m_mesh_merge = m_mesh_merge.cut(float(self.config['flux_floor']))

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

        for key in self.user_setting:
            if self.user_setting[key] != "default":
                if key == 'print_peed':
                    pass  # TODO
                elif key == 'material':
                    pass  # TODO
                elif key == 'raft':
                    if self.user_setting[key] == '0':
                        self.config['raft_layers'] = '0'
                    elif self.user_setting[key] == '1':
                        self.config['raft_layers'] = '4'  # TODO?
                elif key == 'support':
                    self.config['support_material'] = self.user_setting[key]
                elif key == 'layer_height':
                    self.config['first_layer_height'] = self.user_setting[key]
                    self.config['layer_height'] = self.user_setting[key]
                elif key == 'infill':
                    fill_density = float(self.user_setting[key]) * 100
                    fill_density = max(min(fill_density, 99), 0)
                    self.config['fill_density'] = str(fill_density) + '%'
                elif key == 'traveling_speed':
                    self.config['travel_speed'] = self.user_setting[key]
                elif key == 'extruding_speed':
                    self.config['perimeter_speed'] = self.user_setting[key]
                    self.config['infill_speed'] = self.user_setting[key]
                elif key == 'temperature':
                    self.config['temperature'] = self.user_setting[key]
                    self.config['first_layer_temperature'] = self.user_setting[key] + 5

        self.my_ini_writer(tmp_slic3r_setting_file, self.config)

        command += ['--load', tmp_slic3r_setting_file]

        logger.debug('command: ' + ' '.join(command))

        fail_flag = False

        p = subprocess.Popen(command, stderr=subprocess.STDOUT, stdout=subprocess.PIPE, universal_newlines=True)
        progress = 0.2
        slic3r_error = False
        while p.poll() is None:
            line = p.stdout.readline()
            logger.debug(line.rstrip())
            sys.stderr.flush()
            if line:
                if line.startswith('=> ') and not line.startswith('=> Exporting'):
                    progress += 0.12
                    ws.send_progress((line.rstrip())[3:], progress)
                elif "Unable to close this loop" in line:
                    slic3r_error = True
                slic3r_out = line.strip()
        if p.poll() != 0:
            fail_flag = True

        # TODO: design a intermedia data structure for gcode and write a general preprocessor
        if self.config['flux_raft'] == '1':
            m_preprocessor = Raft()
            raft_output = StringIO()
            m_preprocessor.main(tmp_gcode_file, raft_output, debug=False)
            raft_output = raft_output.getvalue()
            with open(tmp_gcode_file, 'w') as f:  # overwrite the file
                print(raft_output, file=f)

        # analying gcode(even transform)
        ws.send_progress('Analyzing Metadata', 0.99)

        fcode_output = BytesIO()
        with open(tmp_gcode_file, 'r') as f:
            m_GcodeToFcode = GcodeToFcode(ext_metadata=self.ext_metadata)
            m_GcodeToFcode.config = self.config
            m_GcodeToFcode.image = self.image
            m_GcodeToFcode.process(f, fcode_output)

            self.path = m_GcodeToFcode.path
            metadata = m_GcodeToFcode.md
            metadata = [float(metadata['TIME_COST']), float(metadata['FILAMENT_USED'].split(',')[0])]
            if slic3r_error or len(m_GcodeToFcode.empty_layer) > 0:
                ws.send_warning("{} empty layers, might be error when slicing {}".format(len(m_GcodeToFcode.empty_layer), repr(m_GcodeToFcode.empty_layer)))

            if float(m_GcodeToFcode.md['MAX_R']) >= HW_PROFILE['model-1']['radius']:
                fail_flag = True
                slic3r_out = "gcode area too big"
            del m_GcodeToFcode

        if output_type == '-g':
            with open(tmp_gcode_file, 'rb') as f:
                output = f.read()
        elif output_type == '-f':
            output = fcode_output.getvalue()
        else:
            raise('wrong output type, only support gcode and fcode')

        ##################### fake code ###########################
        if environ.get("flux_debug") == '1':
            with open('output.gcode', 'wb') as f:
                with open(tmp_gcode_file, 'rb') as f2:
                    f.write(f2.read())

            with open(tmp_stl_file, 'rb') as f:
                with open('merged.stl', 'wb') as f2:
                    f2.write(f.read())

            with open('output.fc', 'wb') as f:
                f.write(fcode_output.getvalue())

            self.my_ini_writer("output.ini", self.config)
        ###########################################################

        # clean up tmp files
        fcode_output.close()
        for f in [tmp_stl_file, tmp_gcode_file, tmp_slic3r_setting_file]:
            try:
                remove(f)
            except:
                pass
        if fail_flag:
            return False, slic3r_out
        else:
            return output, metadata

    def begin_slicing(self, names, ws, output_type):
        """
        input: names of stl that need to be sliced
        output:
            if success:
                gcode (binary in bytes), metadata([TIME_COST, FILAMENT_USED])
            else:
                False, [error message]
        """
        # check if names are all seted
        for n in names:
            if not (n in self.models and n in self.parameter):
                return False, '%s not set yet' % (n)
        # tmp files
        if platform.platform().startswith("Windows"):
            if not os.path.isdir('C:\Temp'):
                os.mkdir('C:\Temp')
            temp_dir = 'C:\Temp'
        else:
            temp_dir = None

        tmp = tempfile.NamedTemporaryFile(dir=temp_dir, suffix='.stl', delete=False)
        tmp_stl_file = tmp.name  # store merged stl

        tmp = tempfile.NamedTemporaryFile(dir=temp_dir, suffix='.gcode', delete=False)
        tmp_gcode_file = tmp.name  # store gcode

        tmp = tempfile.NamedTemporaryFile(dir=temp_dir, suffix='.ini', delete=False)
        tmp_slic3r_setting_file = tmp.name  # store gcode

        m_mesh_merge = _printer.MeshObj([], [])
        for n in names:
            points, faces = self.read_stl(self.models[n])
            m_mesh = _printer.MeshObj(points, faces)
            m_mesh.apply_transform(self.parameter[n])
            m_mesh_merge.add_on(m_mesh)

        bounding_box = m_mesh_merge.bounding_box()
        cx, cy = (bounding_box[0][0] + bounding_box[1][0]) / 2., (bounding_box[0][1] + bounding_box[1][1]) / 2.
        m_mesh_merge.write_stl(tmp_stl_file)

        for key in self.user_setting:
            if self.user_setting[key] != "default":
                if key == 'print_speed':
                    pass  # TODO
                elif key == 'material':
                    pass  # TODO
                elif key == 'raft':
                    if self.user_setting[key] == '0':
                        self.config['raft_layers'] = '0'
                    elif self.user_setting[key] == '1':
                        self.config['raft_layers'] = '4'  # TODO?
                elif key == 'support':
                    self.config['support_material'] = self.user_setting[key]
                elif key == 'layer_height':
                    self.config['first_layer_height'] = self.user_setting[key]
                    self.config['layer_height'] = self.user_setting[key]
                elif key == 'infill':
                    fill_density = float(self.user_setting[key]) * 100
                    fill_density = max(min(fill_density, 99), 0)
                    self.config['fill_density'] = str(fill_density) + '%'
                elif key == 'traveling_speed':
                    self.config['travel_speed'] = self.user_setting[key]
                elif key == 'extruding_speed':
                    self.config['perimeter_speed'] = self.user_setting[key]
                    self.config['infill_speed'] = self.user_setting[key]
                elif key == 'temperature':
                    self.config['temperature'] = self.user_setting[key]
                    self.config['first_layer_temperature'] = self.user_setting[key] + 5

        self.my_ini_writer(tmp_slic3r_setting_file, self.config, delete=['flux_', 'detect_'])

        command = [self.slic3r, tmp_stl_file]
        command += ['--output', tmp_gcode_file]
        command += ['--print-center', '%f,%f' % (cx, cy)]
        command += ['--load', tmp_slic3r_setting_file]

        logger.debug('command: ' + ' '.join(command))
        self.end_slicing()

        # parent_pipe, child_pipe = Pipe()
        pipe = []
        p = Thread(target=self.slicing_worker, args=(command[:], dict(self.config), self.image, dict(self.ext_metadata), output_type, pipe, len(self.working_p)))
        self.working_p.append([p, [tmp_stl_file, tmp_gcode_file, tmp_slic3r_setting_file], pipe])
        p.start()

    def slicing_worker(self, command, config, image, ext_metadata, output_type, child_pipe, p_index):
        tmp_gcode_file = command[3]
        fail_flag = False
        subp = subprocess.Popen(command, stderr=subprocess.STDOUT, stdout=subprocess.PIPE, universal_newlines=True)

        self.working_p[p_index].append(subp)
        # p2 = subprocess.Popen(['osascript', pkg_resources.resource_filename("fluxclient", "printer/hide.AppleScript")], stderr=subprocess.STDOUT, stdout=subprocess.PIPE, universal_newlines=True)
        progress = 0.2
        slic3r_error = False
        slic3r_out = ''
        while subp.poll() is None:
            line = subp.stdout.readline()
            logger.debug(line.rstrip())
            if line:
                if line.startswith('=> ') and not line.startswith('=> Exporting'):
                    progress += 0.12
                    child_pipe.append('{"status": "computing", "message": "%s", "percentage": %.2f}' % ((line.rstrip())[3:], progress))
                elif "Unable to close this loop" in line:
                    slic3r_error = True
                if line.strip():
                    slic3r_out = line
        if subp.poll() != 0:
            fail_flag = True

        if config['flux_raft'] == '1':
            m_preprocessor = Raft()
            raft_output = StringIO()
            m_preprocessor.main(tmp_gcode_file, raft_output, debug=False)
            raft_output = raft_output.getvalue()
            with open(tmp_gcode_file, 'w') as f:  # overwrite the file
                print(raft_output, file=f)

        if not fail_flag:
            # analying gcode(even transform)
            child_pipe.append('{"status": "computing", "message": "analyzing metadata", "percentage": 0.99}')

            fcode_output = BytesIO()

            if config['detect_filament_runout'] == '1':
                ext_metadata['FILAMENT_DETECT'] = 'Y'
            else:
                ext_metadata['FILAMENT_DETECT'] = 'N'

            tmp = 8191
            if config['detect_head_tilt'] == '0':
                tmp -= 32
            if config['detect_head_shake'] == '0':
                tmp -= 16
            ext_metadata['HEAD_ERROR_LEVEL'] = str(tmp)

            with open(tmp_gcode_file, 'r') as f:
                m_GcodeToFcode = GcodeToFcode(ext_metadata=ext_metadata)
                m_GcodeToFcode.config = config
                m_GcodeToFcode.image = image
                m_GcodeToFcode.process(f, fcode_output)
                path = m_GcodeToFcode.path
                metadata = m_GcodeToFcode.md
                metadata = [float(metadata['TIME_COST']), float(metadata['FILAMENT_USED'].split(',')[0])]
                if slic3r_error or len(m_GcodeToFcode.empty_layer) > 0:
                    child_pipe.append('{"status": "warning", "message" : "%s"}' % ("{} empty layers, might be error when slicing {}".format(len(m_GcodeToFcode.empty_layer), repr(m_GcodeToFcode.empty_layer))))

                if float(m_GcodeToFcode.md['MAX_R']) >= HW_PROFILE['model-1']['radius']:
                    fail_flag = True
                    slic3r_out = "gcode area too big"

                del m_GcodeToFcode

            if output_type == '-g':
                with open(tmp_gcode_file, 'rb') as f:
                    output = f.read()
            elif output_type == '-f':
                output = fcode_output.getvalue()
            else:
                raise('wrong output type, only support gcode and fcode')

            ##################### fake code ###########################
            if environ.get("flux_debug") == '1':
                with open('output.gcode', 'wb') as f:
                    with open(tmp_gcode_file, 'rb') as f2:
                        f.write(f2.read())
                tmp_stl_file = command[1]
                with open(tmp_stl_file, 'rb') as f:
                    with open('merged.stl', 'wb') as f2:
                        f2.write(f.read())

                with open('output.fc', 'wb') as f:
                    f.write(fcode_output.getvalue())

                StlSlicer.my_ini_writer("output.ini", config)
            ###########################################################

            # # clean up tmp files
            fcode_output.close()
        if fail_flag:
            child_pipe.append([False, slic3r_out, []])
        else:
            child_pipe.append([output, metadata, path])

    def end_slicing(self):
        """
        when being called, end every working slic3r process
        but couldn't kill the thread
        """
        for p in self.working_p:
            if type(p[-1]) == (subprocess.Popen):
                if p[-1].poll() is None:
                    p[-1].terminate()
            else:
                pass
            for filename in p[1]:
                try:
                    remove(filename)
                except:
                    pass
        # self.working_p = []

    def report_slicing(self):
        """
        report the slicing state
        find the last working process(self.working_p)
        and return the message in it
        """
        ret = []
        if self.working_p:
            l = len(self.working_p[-1][2])
            for _ in range(l):
                message = self.working_p[-1][2][0]
                self.working_p[-1][2].pop(0)
                if type(message) == str:
                    ret.append(message)
                else:
                    if message[0]:
                        self.output = message[0]
                        self.metadata = message[1]
                        self.path = message[2]
                        m = '{"status": "complete", "length": %d, "time": %.3f, "filament_length": %.2f}' % (len(self.output), self.metadata[0], self.metadata[1])

                    else:
                        self.output = None
                        self.metadata = None
                        self.path = None
                        m = '{"status": "error", "error": "%s"}' % message[1]
                    ret.append(m)
        return ret

    @classmethod
    def my_ini_parser(cls, data):
        """
        data[in]: [str] indicating a file path or [list of str] indicating lines of ini file
        read-in .ini file setting file as default settings
        return a dict
        """
        result = {}
        if type(data) == str:
            # file path
            f = open(data, 'r')
            lines = f.readlines()
        else:
            lines = data

        for i in lines:
            if i[0] == '#':
                pass
            elif '=' in i:
                tmp = i.rstrip().split('=')
                result[tmp[0].strip()] = tmp[1].strip()
            else:
                logger.error(i)
                raise ValueError('not ini file?')
        return result

    def ini_value_check(self, key, value):
        """
        key[in]: str
        value[out]: str
        return: 'ok' or [error message]
        check whether (key, value) pair is valid according to the constraint
        """
        if key in self.config:
            if value.strip() == 'default':
                return 'ok'
            if ini_constraint[key]:
                return ini_constraint[key][0](key, value, *ini_constraint[key][1:])
            else:
                return 'ok'
        else:
            return 'key not exist: %s' % key

    @classmethod
    def my_ini_writer(cls, file_path, content, delete=None):
        """
        file_path[in]: str, output file_path
        content[in]: dict
        write a .ini file
        """
        with open(file_path, 'w') as f:
            for i in content:
                if delete and any(j in i for j in delete):
                    pass
                else:
                    print("%s=%s" % (i, content[i]), file=f)
        return

    @classmethod
    def ascii_or_binary(cls, data, byte_order):
        """
        data[in]: bytes data of a stl file
        byte_order[in]: '@' or '<', for different input source
        check what kind of stl file it is
        return False -> binary
        return True -> ascii
        """
        if not data.startswith(b'solid '):
            return False

        length = unpack(byte_order + 'I', data[80:84])[0]
        if len(data) == 80 + 4 + length * 50:
            return False
        return True

    @classmethod
    def read_stl(cls, file_data):
        """
        file_data[in]: string indicating a a file path, or a bytes that is the content of stl file
        read in stl
        """
        # ref: https://en.wikipedia.org/wiki/STL_(file_format)
        if type(file_data) == str:
            with open(file_data, 'rb') as f:
                file_data = f.read()
                byte_order = '@'
        elif type(file_data) == bytes:
            byte_order = '<'
        else:
            raise ValueError('wrong stl data type: %s' % str(type(file_data)))
        points = {}  # key: points, value: index
        faces = []
        counter = 0
        if cls.ascii_or_binary(file_data, byte_order):
            # ascii stl file
            instl = StringIO(file_data.decode('utf8'))
            instl.readline()  # read in: "solid [name]"

            while True:
                t = instl.readline()  # read in: "facet normal 0 0 0"
                if t[:8] != 'endsolid':  # end of file
                    read_normal = tuple(map(float, (t.split()[-3:])))
                    instl.readline()   # outer loop
                    v0 = tuple(map(float, (instl.readline().split()[-3:])))
                    v1 = tuple(map(float, (instl.readline().split()[-3:])))
                    v2 = tuple(map(float, (instl.readline().split()[-3:])))
                    right_hand_mormal = normalize(normal([v0, v1, v2]))
                    if dot(right_hand_mormal, read_normal) < 0:
                        v1, v2 = v2, v1

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
            length = unpack(byte_order + 'I', file_data[80:84])[0]

            patten = byte_order + 'fff'
            index = 84
            for i in range(length):
                read_normal = unpack(patten, file_data[index + (4 * 3 * 0):index + (4 * 3 * 1)])
                v0 = unpack(patten, file_data[index + (4 * 3 * 1):index + (4 * 3 * 2)])
                v1 = unpack(patten, file_data[index + (4 * 3 * 2):index + (4 * 3 * 3)])
                v2 = unpack(patten, file_data[index + (4 * 3 * 3):index + (4 * 3 * 4)])
                right_hand_mormal = normalize(normal([v0, v1, v2]))
                if dot(right_hand_mormal, read_normal) < 0:
                    v1, v2 = v2, v1

                face = []
                for v in [v0, v1, v2]:
                    if v not in points:
                        points[v] = counter
                        points[counter] = v
                        counter += 1
                    face.append(points[v])
                faces.append(face)
                index += 50

        points_list = []
        for i in range(counter):
            points_list.append(points[i])
        return points_list, faces
