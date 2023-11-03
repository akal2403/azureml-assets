# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Register preset model."""

import json
import time
import requests
from random import randint

from azureml.acft.common_components import get_logger_app

from azureml.core.run import Run, _OfflineRun

from azureml._model_management._util import get_requests_session
from azureml._restclient.clientbase import ClientBase


logger = get_logger_app(__name__)


def get_model_path_in_HOBO_storage(run_details) -> str:
    """Get model HOBO path from run document.

    Args:
        run_details (_type_): Run details

    Raises:
        Exception: throw exception if cannot get model path

    Returns:
        str: model path in datastore
    """
    try:
        run_id = run_details['runId']
        model_path_in_storage = run_details['runDefinition'][
            'outputData']['output_model']['outputLocation']['uri']['path']
        model_path_in_storage = model_path_in_storage.replace(
            "${{name}}", run_id)
        return model_path_in_storage
    except Exception:
        logger.warning(
            "cannot fetch model output path from properties, ES should set it.")
        raise Exception(
            "Unable to find model output path from rundocument, ES should set it.")


def get_modelregistry_url(workspace):
    """Get model registry url."""
    from azureml._restclient.assets_client import AssetsClient

    assets_client = AssetsClient(workspace.service_context)
    modelregistry_url = assets_client.get_cluster_url()
    uri = (
        "/modelmanagement/{}/subscriptions/{}/resourceGroups/{}/providers/"
        "Microsoft.MachineLearningServices/workspaces/{}/models".format(
            "v1.0", workspace.subscription_id, workspace.resource_group, workspace.name
        )
    )
    return modelregistry_url + uri


def is_model_registered_already(request_uri, model_properties, params, headers):
    """Check if the model is already registered."""
    current_run_id = model_properties["runId"]
    model_name = model_properties["name"]
    model_version = model_properties["version"]
    url = request_uri + "/" + model_name + ":" + model_version
    try:
        resp = ClientBase._execute_func(
            get_requests_session().get, url, params=params, headers=headers)
        resp.raise_for_status()
    except requests.exceptions.HTTPError:
        # log any HTTP errors and return False
        logger.error(
            "Received bad response from GET Model request:\n"
            "Response Code: {}\n"
            "Headers: {}\n"
            "Content: {}".format(resp.status_code, resp.headers, resp.content)
        )
        return False
    # check if model is registered with the same runId as current.
    # if it is then model registration succeeded in past so return True.
    # if not then some other model is registered with same name and version so return False.
    return "runId" in resp.json() and resp.json()["runId"] == current_run_id


def submit_rest_request(
    request_type,
    request_uri,
    request_body,
    params=None,
    headers=None,
    use_auto_version=False,
):
    """Register model rest API."""
    # TODO: This relies on the v1 SDK. Handling will need to be adapted to shift to v2

    # retry_count = RETRY_NUM_FOR_409 if use_auto_version else 1
    retry_count = 3 if use_auto_version else 1
    i = 0
    while True:
        logger.info(f"Attempt number {i + 1} for model registration.")
        try:
            resp = ClientBase._execute_func(
                request_type,
                request_uri,
                params=params,
                headers=headers,
                json=request_body,
            )
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError:
            # check if model has been registered already
            if resp.status_code == 409:
                logger.warning(
                    "This model id (name:version combination) already exists."
                )
                logger.info(
                    "Checking if this model has already been registered by current RunId previous attempts/retries."
                )
                if not use_auto_version and is_model_registered_already(
                    request_uri, request_body, params, headers
                ):
                    logger.info(
                        "Good news! This model has already been registered by current RunId."
                    )
                    return resp
                i += 1
                logger.error(
                    "This model id has already been used for a different model registration."
                )
                if i == retry_count:
                    logger.error("Exceeded all retry attempts.")
                else:
                    logger.error(
                        "Attempting to retry again with autoVersion set to True."
                    )
                    time.sleep(
                        randint(1000, 5000) / 1000
                    )  # sleep randomly from 1 second to 5 seconds
                    continue

            raise Exception(
                "Received bad response from POST Model request:\n"
                "Response Code: {}\n"
                "Headers: {}\n"
                "Content: {}".format(
                    resp.status_code, resp.headers, resp.content)
            )


def register_model(
    workspace,
    model_name,
    model_version,
    model_output_path,
    run_id,
    tags=None,
    properties=None,
    description=None,
    model_format="PRESETS",
):
    """Register a model with the provided workspace.

    :param workspace: The workspace to register the model with.
    :type workspace: azureml.core.Workspace
    :param model_name: The name to register the model with.
    :type model_name: str
    :param model_version: The version to register the model with. If it is None, the code figures out the version.
    :type model_version: int or None
    :param model_output_path: The path of the model output.
    :type model_output_path: str
    :param tags: An optional dictionary of key value tags to assign to the model.
    :type tags: dict({str : str})
    :param properties: An optional dictionary of key value properties to assign to the model.
        These properties can't be changed after model creation, however new key value pairs can be added.
    :type properties: dict({str : str})
    :param description: A text description of the model.
    :type description: str
    :param model_format: The storage format for this model.
    :type description: str
    :return: The registered model json string.
    :rtype: str
    """
    if tags:
        try:
            if not isinstance(tags, dict):
                raise ValueError("Tags must be a dict")
            tags = json.loads(json.dumps(tags))
        except ValueError:
            raise ValueError(
                "Error with JSON serialization for tags, "
                "be sure they are properly formatted."
            )
    if properties:
        try:
            if not isinstance(properties, dict):
                raise ValueError("Properties must be a dict")
            properties = json.loads(json.dumps(properties))
        except ValueError:
            raise ValueError(
                "Error with JSON serialization for properties, "
                "be sure they are properly formatted."
            )

    request_url = get_modelregistry_url(workspace)

    request_body = {
        "name": model_name,
        "description": description,
        "url": model_output_path,
        "runId": run_id,
        "mimeType": "application/x-python",
        "properties": properties,
        "kvTags": tags,
        "modelFormat": model_format,
    }

    use_auto_version = model_version is None

    request_params = {"autoVersion": "true" if use_auto_version else "false"}
    if not use_auto_version:
        request_body["version"] = model_version

    request_headers = {"Content-Type": "application/json"}

    request_headers.update(workspace._auth_object.get_authentication_header())

    logger.info("Starting register model request")
    logger.info(f"Request body: {request_body}")
    print(f"Request url is {request_url}, Request body: {request_body}")
    resp = submit_rest_request(
        get_requests_session().post,
        request_url,
        request_body,
        request_params,
        request_headers,
        use_auto_version=use_auto_version,
    )
    logger.info("Done register model request")
    resp_json = resp.json()
    if "name" in resp_json and "version" in resp_json:
        logger.info(
            f"Registered model {resp_json['name']} with version {resp_json['version']}"
        )
    return resp_json


def registermodel_entrypoint(
    args,
    model_name,
    finetune_run_id,
    finetuned_model_input,
    registered_model_output,
    registered_model_version=None,
    properties=None,
):
    """Entry point for model registration for presets."""
    logger.info("Starting register presets model")

    # model_name = os.environ.get("AZUREML_PARAMETER_registered_model_name")
    # if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9\-\._]{0,254}', model_name):
    #     raise UserErrorException("Invalid registered model name. The name of a model may only contain "
    #                              "alphanumeric characters, '-', '.', '_', and be a maximum of 255 characters long.")
    try:
        run = Run.get_context()
        if isinstance(run, _OfflineRun):
            raise ValueError("Register model is not supported for Offline run")

        # run_dto = run._client.get_run()
        run_details = run.get_details()
        # run_properties = run.get_properties()

        ws = run._experiment.workspace

        model_output_path = get_model_path_in_HOBO_storage(run_details)
        # get pipeline run id
        run_id = run_details["runId"]
        top_level_run = run
        while top_level_run.parent:
            top_level_run = top_level_run.parent

    except Exception as e:
        logger.error("Can't identify output location")
        logger.error(f"args: {e.args}, traceback: {e.__traceback__}")
        raise e

    # get the dependent components configs
    # finetuned_model_config = get_params_from_pipeline_config(finetuned_model_input)

    # keyname_basis_postprocess_type = get_keyname_basis_postprocess_type(finetuned_model_config)

    # register_model_type = get_model_type(finetuned_model_input, finetuned_model_config, is_v2)

    # manifest_dict_enginecontroller = {}
    # storage_item_enginecontroller = []
    # manifest_dict_pipe = {}
    # storage_item_pipe = []
    # model_details = {}
    # rootdir = finetuned_model_input
    # dataset_root_path = os.path.join("{0}", dataset_path)
    # model_directory_path = "model"

    # for subdir, dirs, files in os.walk(rootdir):
    #     for file in files:
    #         if check_sub_directory_has_required_model_folder(subdir):
    #             model_type = subdir.split("/")[-1] if subdir.split("/")[-1] not in MODEL_FOLDER_NAME else ""
    #             remote_location = os.path.join(dataset_root_path, model_directory_path, model_type, file)
    #             local_path = os.path.join(model_type, file)
    #             blob_dict = {"remoteLocation": remote_location, "localRelativePath": local_path, "unpackType": 0}
    #             storage_item_pipe.append(blob_dict)
    #             if file.endswith(".json"):
    #                 storage_item_enginecontroller.append(blob_dict)

    # if register_model_type == ModelType.BASE_PLUS_LORA and run.parent.id.startswith("sub-") and \
    #             len(storage_item_pipe) == 0 and len(storage_item_enginecontroller) == 0:
    #     logging.warning("We have identified a registration attempt for an empty base+lora model within a subgraph. \
    #                      Exiting component with no-op.")
    #     return 0

    # if len(storage_item_pipe) == 0:
    #     raise UserErrorException("Pipe manifest is empty.")
    # if len(storage_item_enginecontroller) == 0:
    #     raise UserErrorException("Engine controller manifest is empty.")

    # manifest_dict_pipe["storageItems"] = storage_item_pipe
    # manifest_dict_enginecontroller["storageItems"] = storage_item_enginecontroller

    # output_manifest_pipe = registered_model_output + "/manifest.pipe.json"
    # with open(output_manifest_pipe, 'w') as f:
    #     json.dump(manifest_dict_pipe, f)

    # output_manifest_enginecontroller = registered_model_output + "/manifest.enginecontroller.json"
    # with open(output_manifest_enginecontroller, 'w') as f:
    #     json.dump(manifest_dict_enginecontroller, f)

    # # register model
    # model_json_path = os.path.join(finetuned_model_input, "model.json")
    # base_model_name = get_model_engine(model_json_path).lower()

    # # Manifest file for engine/controller and pipe
    engine_controller_manifest_path = (
        "azureml/{}/output_model/manifest.enginecontroller.json".format(run_id)
    )
    pipe_manifest_path = "azureml/{}/output_model/manifest.pipe.json".format(
        run_id)

    # Model properties is a dict({str : str}), all values must be converted to string type.
    properties = properties or {}
    properties.update(
        {
            "pipeManifestPath": pipe_manifest_path,
            "intellectualPropertyPublisher": "OSS",
            # "modelPath": model_directory_path,
            # "componentVersion": "1",
            "engineControllerManifestPath": engine_controller_manifest_path,
            # "azureMlBaseModel": base_model_name,
            # "loraDim": str(finetuned_model_config["inputs"][keyname_basis_postprocess_type]["arguments"]["lora_dim"])
        }
    )

    # # base model weights assetId
    # base_weights_asset_id = get_model_asset_id(model_json_path)
    # if base_weights_asset_id:
    #     properties["azureMlBaseModelAssetId"] = base_weights_asset_id

    # ft_component_version = get_ft_component_version(
    #     is_v2,
    #     finetuned_model_config,
    #     run_properties,
    #     keyname_basis_postprocess_type
    # )
    # if ft_component_version:
    #     properties["azureMlFinetuneVersion"] = ft_component_version

    # if register_model_type == ModelType.LORA_ONLY:
    #     properties["hasDeltaWeights"] = "true"
    #     base_weights_id = get_model_engine(model_json_path).lower()
    #     properties["baseWeightsId"] = base_weights_id
    # else:
    #     properties["modelArchitecture"] = get_model_architecture(base_model_name)

    # model_details["modelName"] = model_name

    # logging.info("Registering model with properties: {}".format(properties))

    _ = register_model(
        workspace=ws,
        model_name=model_name,
        model_version=registered_model_version,
        model_output_path=model_output_path,
        run_id=top_level_run.id,
        properties=properties,
        model_format="PRESETS",
    )

    # if 'version' in resp_json:
    #     model_details["modelVersion"] = int(resp_json['version'])
    # else:
    #     # Possible when it is a 409 from a previous registration attempt with same runID
    #     # In this case, must be v1.5 component which has a static model version to register with.
    #     model_details["modelVersion"] = registered_model_version

    # output_model_details = registered_model_output + "/model_details.json"
    # with open(output_model_details, 'w') as f:
    #     json.dump(model_details, f)
