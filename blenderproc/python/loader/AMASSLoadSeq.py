from blenderproc.python.utility.SetupUtility import SetupUtility


import torch
import glob
import json
import os
import random
from datetime import datetime
from typing import List, Tuple

import bpy
import mathutils
import numpy as np

from blenderproc.python.types.MeshObjectUtility import MeshObject
from blenderproc.python.utility.Utility import Utility
from blenderproc.python.loader.ObjectLoader import load_obj
from blenderproc.python.utility.IO import writePC2

def load_AMASS_seq(data_path: str, sub_dataset_id: str, temp_dir: str = None, body_model_gender: str = None,
               subject_id: str = "", sequence_id: int = -1, num_betas: int = 10, num_dmpls: int = 8):
    """
    use the pose parameters to generate the mesh and loads it to the scene.

    :param data_path: The path to the AMASS Dataset folder in resources folder.
    :param sub_dataset_id: Identifier for the sub dataset, the dataset which the human pose object should be extracted from.
                                Available: ['CMU', 'Transitions_mocap', 'MPI_Limits', 'SSM_synced', 'TotalCapture',
                                'Eyes_Japan_Dataset', 'MPI_mosh', 'MPI_HDM05', 'HumanEva', 'ACCAD', 'EKUT', 'SFU', 'KIT', 'H36M', 'TCD_handMocap', 'BML']
    :param temp_dir: A temp directory which is used for writing the temporary .obj file.
    :param body_model_gender: The model gender, pose will represented using male, female or neutral body shape.
                                   Available:[male, female, neutral]. If None is selected a random one is choosen.
    :param subject_id: Type of motion from which the pose should be extracted, this is dataset dependent parameter.
                            If left empty a random subject id is picked.
    :param sequence_id: Sequence id in the dataset, sequences are the motion recorded to represent certain action.
                             If set to -1 a random sequence id is selected.
    :param num_betas: Number of body parameters
    :param num_dmpls: Number of DMPL parameters
    :return: The list of loaded mesh objects.
    """
    if body_model_gender is None:
        body_model_gender = random.choice(["male", "female", "neutral"])
    if temp_dir is None:
        temp_dir = Utility.get_temporary_directory()

    # Install required additonal packages
    SetupUtility.setup_pip(["git+https://github.com/abahnasy/smplx", "git+https://github.com/abahnasy/human_body_prior"])

    # Get the currently supported mocap datasets by this loader
    taxonomy_file_path = os.path.join(data_path, "taxonomy.json")
    supported_mocap_datasets = AMASSLoader._get_supported_mocap_datasets(taxonomy_file_path, data_path)

    # selected_obj = self._files_with_fitting_ids
    sequence_path, main_path = AMASSLoader._get_sequence_path(supported_mocap_datasets, sub_dataset_id, subject_id, sequence_id)
    if os.path.exists(sequence_path):
        # load AMASS dataset sequence file which contains the coefficients for the whole motion sequence
        sequence_body_data = np.load(sequence_path)
        # get the number of supported frames
        no_of_frames_per_sequence = sequence_body_data['poses'].shape[0]
    else:
        raise Exception(
            "Invalid sequence/subject: {} category identifiers, please choose a "
            "valid one. Used path: {}".format(used_subject_id, sequence_path))
    
    pc2_path = os.path.join(main_path,'cache',
        str(sequence_id) + "_" + body_model_gender + '.pc2'
    )
    if not os.path.exists(os.path.join(main_path,'cache')):
        os.mkdir(os.path.join(main_path,'cache'))
    V = np.zeros((no_of_frames_per_sequence, 6890, 3), np.float32)

    bdata = sequence_body_data
    time_length = len(bdata['trans'])
    comp_device = "cpu"
    body_params = {
        'root_orient': torch.Tensor(bdata['poses'][:, :3]).to(comp_device), # controls the global root orientation
        'pose_body': torch.Tensor(bdata['poses'][:, 3:66]).to(comp_device), # controls the body
        'pose_hand': torch.Tensor(bdata['poses'][:, 66:]).to(comp_device), # controls the finger articulation
        'trans': torch.Tensor(bdata['trans']).to(comp_device), # controls the global body position
        'betas': torch.Tensor(np.repeat(bdata['betas'][:num_betas][np.newaxis], repeats=time_length, axis=0)).to(comp_device), # controls the body shape. Body shape is static
        'dmpls': torch.Tensor(bdata['dmpls'][:, :num_dmpls]).to(comp_device) # controls soft tissue dynamics
    }

    if not os.path.isfile(pc2_path):
        body_model, faces = AMASSLoader._load_parametric_body_model(data_path, body_model_gender, num_betas, num_dmpls)
        body_trans_root = body_model(**{k:v for k,v in body_params.items() if k in ['pose_body', 'betas', 'pose_hand', 'dmpls',
                                                               'trans', 'root_orient']})
        V = body_trans_root.v.data.cpu().numpy()
        print("Writing PC2 file...")
        writePC2(pc2_path, V)

class AMASSLoader:
    """
    AMASS is a large database of human motion unifying 15 different optical marker-based motion capture datasets by representing them within a common framework and parameterization. All of the mocap data is convereted into realistic 3D human meshes represented by a rigged body model called SMPL, which provides a standard skeletal representation as well as a fully rigged surface mesh. Warning: Only one part of the AMASS database is currently supported by the loader! Please refer to the AMASSLoader example for more information about the currently supported datasets.

    Any human pose recorded in these motions could be reconstructed using the following parameters: `"sub_dataset_identifier"`, `"sequence id"`, `"frame id"` and `"model gender"` which will represent the pose, these parameters specify the exact pose to be generated based on the selected mocap dataset and motion category recorded in this dataset.

    Note: if this module is used with another loader that loads objects with semantic mapping, make sure the other
    module is loaded first in the config file.
    """

    # hex values for human skin tone to sample from
    human_skin_colors = ['2D221E', '3C2E28', '4B3932', '5A453C', '695046', '785C50', '87675A', '967264', 'A57E6E',
                         'B48A78', 'C39582', 'D2A18C', 'E1AC96', 'F0B8A0', 'FFC3AA', 'FFCEB4', 'FFDABE', 'FFE5C8']


    @staticmethod
    def _get_sequence_path(supported_mocap_datasets: dict, used_sub_dataset_id: str, used_subject_id: str, used_sequence_id: int) -> [str, str]:
        """ Extract pose and shape parameters corresponding to the requested pose from the database to be processed by the parametric model

        :param supported_mocap_datasets: A dict which maps sub dataset names to their paths.
        :param used_sub_dataset_id: Identifier for the sub dataset, the dataset which the human pose object should be extracted from.
        :param used_subject_id: Type of motion from which the pose should be extracted, this is dataset dependent parameter.
        :param used_sequence_id: Sequence id in the dataset, sequences are the motion recorded to represent certain action.
        :return: tuple of arrays contains the parameters. Type: tuple
        """
        # check if the sub_dataset is supported
        if used_sub_dataset_id in supported_mocap_datasets:
            # get path from dictionary
            sub_dataset_path = supported_mocap_datasets[used_sub_dataset_id]
            # concatenate path to specific
            if not used_subject_id:
                # if none was selected
                possible_subject_ids = glob.glob(os.path.join(sub_dataset_path, "*"))
                possible_subject_ids.sort()
                if len(possible_subject_ids) > 0:
                    used_subject_id_str = os.path.basename(random.choice(possible_subject_ids))
                else:
                    raise Exception("No subjects found in folder: {}".format(sub_dataset_path))
            else:
                used_subject_id_str = "{:02d}".format(int(used_subject_id))

            if used_sequence_id < 0:
                # if no sequence id was selected
                possible_sequence_ids = glob.glob(os.path.join(sub_dataset_path, used_subject_id_str, "*"))
                possible_sequence_ids.sort()
                if len(possible_sequence_ids) > 0:
                    used_sequence_id = os.path.basename(random.choice(possible_sequence_ids))
                    used_sequence_id = used_sequence_id[used_sequence_id.find("_")+1:used_sequence_id.rfind("_")]
                else:
                    raise Exception("No sequences found in folder: {}".format(os.path.join(sub_dataset_path,
                                                                                           used_subject_id_str)))
            else:
                used_sequence_id = used_sequence_id
            subject_path = os.path.join(sub_dataset_path, used_subject_id_str)
            used_subject_id_str_reduced = used_subject_id_str[:used_subject_id_str.find("_")] \
                if "_" in used_subject_id_str else used_subject_id_str
            sequence_path = os.path.join(subject_path, used_subject_id_str_reduced +
                                         "_{:02d}_poses.npz".format(int(used_sequence_id)))
            return sequence_path, subject_path
        else:
            raise Exception(
                "The requested mocap dataset is not yest supported, please choose anothe one from the following "
                "supported datasets: {}".format([key for key, value in supported_mocap_datasets.items()]))

    @staticmethod
    def _load_parametric_body_model(data_path: str, used_body_model_gender: str, num_betas: int,
                                    num_dmpls: int) -> Tuple["BodyModel", np.array]:
        """ loads the parametric model that is used to generate the mesh object

        :return:  parametric model. Type: tuple.
        """
        import torch
        from human_body_prior.body_model.body_model import BodyModel

        bm_path = os.path.join(data_path, 'body_models', 'smplh', used_body_model_gender, 'model.npz')  # body model
        dmpl_path = os.path.join(data_path, 'body_models', 'dmpls', used_body_model_gender, 'model.npz')  # deformation model
        if not os.path.exists(bm_path) or not os.path.exists(dmpl_path):
            raise Exception("Parametric Body model doesn't exist, please follow download instructions section in AMASS Example")
        comp_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        body_model = BodyModel(bm_path=bm_path, num_betas=num_betas, num_dmpls=num_dmpls, path_dmpl=dmpl_path).to(comp_device)
        faces = body_model.f.detach().cpu().numpy()
        return body_model, faces

    @staticmethod
    def _get_supported_mocap_datasets(taxonomy_file_path: str, data_path: str) -> dict:
        """ get latest updated list from taxonomoy json file about the supported mocap datasets supported in the loader module and update.supported_mocap_datasets list

        :param taxonomy_file_path: path to taxomomy.json file which contains the supported datasets and their respective paths. Type: string.
        :param data_path: path to the AMASS dataset root folder. Type: string.
        """

        # dictionary contains mocap dataset name and path to its sub folder within the main dataset, dictionary will
        # be filled from taxonomy.json file which indicates the supported datastests
        supported_mocap_datasets = {}
        if os.path.exists(taxonomy_file_path):
            with open(taxonomy_file_path, "r") as f:
                loaded_data = json.load(f)
                for block in loaded_data:
                    if "sub_data_id" in block:
                        sub_dataset_id = block["sub_data_id"]
                        supported_mocap_datasets[sub_dataset_id] = os.path.join(data_path, block["path"])
        else:
            raise Exception("The taxonomy file could not be found: {}".format(taxonomy_file_path))

        return supported_mocap_datasets