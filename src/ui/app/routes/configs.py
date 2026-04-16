from json import JSONDecodeError, loads
from re import match
from time import time
from typing import Dict, List, Literal, Optional

from flask import Blueprint, redirect, render_template, request, url_for
from flask_login import login_required
from werkzeug.utils import secure_filename

from app.dependencies import API_CLIENT, BW_CONFIG, CONFIG_TASKS_EXECUTOR, DATA
from app.utils import flash, is_editable_method

from app.routes.utils import handle_error, verify_data_in_form, wait_applying

configs = Blueprint("configs", __name__)

CONFIG_TYPES = {
    "HTTP": {"context": "global", "description": "Configurations at the HTTP level of NGINX."},
    "SERVER_HTTP": {"context": "multisite", "description": "Configurations at the HTTP/Server level of NGINX."},
    "DEFAULT_SERVER_HTTP": {
        "context": "global",
        "description": 'Configurations at the Server level of NGINX, specifically for the "default server" when the supplied client name doesn\'t match any server name in SERVER_NAME.',
    },
    "MODSEC_CRS": {"context": "multisite", "description": "Configurations applied before the OWASP Core Rule Set is loaded."},
    "MODSEC": {
        "context": "multisite",
        "description": "Configurations applied after the OWASP Core Rule Set is loaded, or used when the Core Rule Set is not loaded.",
    },
    "STREAM": {"context": "global", "description": "Configurations at the Stream level of NGINX."},
    "SERVER_STREAM": {"context": "multisite", "description": "Configurations at the Stream/Server level of NGINX."},
    "DEFAULT_SERVER_STREAM": {
        "context": "global",
        "description": 'Configurations at the Stream/Server level of NGINX, specifically for the "default server" when the supplied client name doesn\'t match any server name in SERVER_NAME.',
    },
    "CRS_PLUGINS_BEFORE": {"context": "multisite", "description": "Configurations applied before the OWASP Core Rule Set plugins are loaded."},
    "CRS_PLUGINS_AFTER": {"context": "multisite", "description": "Configurations applied after the OWASP Core Rule Set plugins are loaded."},
}


def _use_modsecurity_global_crs() -> bool:
    value = BW_CONFIG.get_config(
        global_only=True,
        methods=False,
        with_drafts=True,
        filtered_settings=("USE_MODSECURITY_GLOBAL_CRS",),
    ).get("USE_MODSECURITY_GLOBAL_CRS", "no")
    return str(value).lower() == "yes"


@configs.route("/configs", methods=["GET"])
@login_required
def configs_page():
    service = request.args.get("service", "")
    config_type = request.args.get("type", "")
    return render_template(
        "configs.html",
        configs=API_CLIENT.get_configs(with_drafts=True),
        services=BW_CONFIG.get_config(global_only=True, methods=False, with_drafts=True, filtered_settings=("SERVER_NAME",))["SERVER_NAME"],
        db_templates=" ".join([template for template in API_CLIENT.get_templates() if template != "ui"]),
        config_service=service,
        config_type=config_type,
    )


@configs.route("/configs/convert", methods=["POST"])
@login_required
def configs_convert():
    if API_CLIENT.readonly:
        return handle_error("Database is in read-only mode", "configs")

    verify_data_in_form(
        data={"configs": None},
        err_message="Missing configs parameter on /configs/convert.",
        redirect_url="configs",
        next=True,
    )
    verify_data_in_form(
        data={"convert_to": None},
        err_message="Missing convert_to parameter on /configs/convert.",
        redirect_url="configs",
        next=True,
    )

    raw_configs = request.form["configs"]
    if not raw_configs:
        return handle_error("No configs selected.", "configs", True)
    try:
        configs = loads(raw_configs)
    except JSONDecodeError:
        return handle_error("Invalid configs parameter on /configs/convert.", "configs", True)

    convert_to = request.form["convert_to"]
    if convert_to not in ("online", "draft"):
        return handle_error("Invalid convert_to parameter.", "configs", True)
    DATA.load_from_file()

    def convert_configs(configs: List[Dict[str, str]], convert_to: str):
        wait_applying()

        api_configs = API_CLIENT.get_configs(with_drafts=True)
        db_config_map = {}
        for c in api_configs:
            sid = c.get("service") or None
            sid = None if sid in ("global", "") else sid
            db_config_map[(sid, c["type"], c["name"])] = c

        configs_to_convert = set()
        non_editable_configs = set()
        non_convertible_configs = set()
        missing_configs = set()

        for config in configs:
            service_id = config.get("service") or None
            service_id = None if service_id in ("global", "") else service_id
            config_type = config.get("type", "").strip().replace("-", "_").lower()
            name = config.get("name")
            key = (service_id, config_type, name)
            db_config = db_config_map.get(key)
            config_label = f"{config_type}/{name}{f' for service {service_id}' if service_id else ''}"
            if not db_config:
                missing_configs.add(config_label)
                continue
            if db_config.get("template") or not is_editable_method(db_config.get("method")):
                non_editable_configs.add(config_label)
                continue
            if db_config.get("is_draft", False) == (convert_to == "draft"):
                non_convertible_configs.add(config_label)
                continue
            configs_to_convert.add(key)

        for non_editable_config in non_editable_configs:
            DATA["TO_FLASH"].append(
                {"content": f"Custom config {non_editable_config} is not a UI/API custom config and will not be converted.", "type": "error"}
            )

        for non_convertible_config in non_convertible_configs:
            DATA["TO_FLASH"].append(
                {"content": f"Custom config {non_convertible_config} is already a {convert_to} config and will not be converted.", "type": "error"}
            )

        for missing_config in missing_configs:
            DATA["TO_FLASH"].append({"content": f"Custom config {missing_config} could not be found.", "type": "error"})

        if not configs_to_convert:
            DATA["TO_FLASH"].append(
                {"content": "All selected custom configs could not be found, are not UI/API custom configs or are already converted.", "type": "error"}
            )
            DATA.update({"RELOADING": False, "CONFIG_CHANGED": False})
            return

        for key in configs_to_convert:
            db_config = db_config_map[key]
            service_id = db_config.get("service") or None
            service_id = None if service_id in ("global", "") else service_id
            try:
                API_CLIENT.update_config(
                    service_id,
                    db_config["type"],
                    db_config["name"],
                    is_draft=convert_to == "draft",
                )
            except Exception as e:
                DATA["TO_FLASH"].append({"content": f"An error occurred while saving the custom configs: {e}", "type": "error"})
                DATA.update({"RELOADING": False, "CONFIG_CHANGED": False})
                return

        converted_labels = [f"{config[1]}/{config[2]}{f' for service {config[0]}' if config[0] else ''}" for config in configs_to_convert]
        DATA["TO_FLASH"].append({"content": f"Converted to \"{convert_to.title()}\" configs: {', '.join(converted_labels)}", "type": "success"})
        DATA["RELOADING"] = False

    DATA.update({"RELOADING": True, "LAST_RELOAD": time(), "CONFIG_CHANGED": True})
    CONFIG_TASKS_EXECUTOR.submit(convert_configs, configs, convert_to)

    return redirect(
        url_for(
            "loading",
            next=url_for("configs.configs_page"),
            message=f"Converting config{'s' if len(configs) > 1 else ''} to {convert_to}",
        )
    )


@configs.route("/configs/delete", methods=["POST"])
@login_required
def configs_delete():
    if API_CLIENT.readonly:
        return handle_error("Database is in read-only mode", "configs")

    verify_data_in_form(
        data={"configs": None},
        err_message="Missing configs parameter on /configs/delete.",
        redirect_url="configs",
        next=True,
    )
    configs = request.form["configs"]
    if not configs:
        return handle_error("No configs selected.", "configs", True)
    try:
        configs = loads(configs)
    except JSONDecodeError:
        return handle_error("Invalid configs parameter on /configs/delete.", "configs", True)
    DATA.load_from_file()

    def delete_configs(configs: Dict[str, str]):
        wait_applying()

        api_configs = API_CLIENT.get_configs(with_drafts=True, with_data=True)
        configs_to_delete = set()
        non_editable_configs = set()
        remaining_configs_by_method: Dict[str, List[Dict[str, str]]] = {"ui": [], "api": []}

        for api_config in api_configs:
            config_service_id = api_config.get("service") or None
            config_service_id = None if config_service_id in ("global", "") else config_service_id
            key = f"{(config_service_id + '/') if config_service_id else ''}{api_config['type']}/{api_config['name']}"
            keep = True
            for config in configs:
                config_service = config["service"] if config["service"] != "global" else None
                if api_config["name"] == config["name"] and config_service_id == config_service and api_config["type"] == config["type"]:
                    config_method = api_config.get("method")
                    if config_method not in remaining_configs_by_method:
                        non_editable_configs.add(key)
                        continue
                    configs_to_delete.add(key)
                    keep = False
                    break
            if api_config.get("template") or not keep:
                continue
            config_without_template = {k: v for k, v in api_config.items() if k != "template"}
            config_without_template["service_id"] = config_service_id
            config_without_template.pop("service", None)
            config_method = config_without_template.get("method")
            if config_method in remaining_configs_by_method:
                remaining_configs_by_method[config_method].append(config_without_template)

        for non_editable_config in non_editable_configs:
            DATA["TO_FLASH"].append(
                {
                    "content": f"Custom config {non_editable_config} is not a UI/API custom config and will not be deleted.",
                    "type": "error",
                }
            )

        if not configs_to_delete:
            DATA["TO_FLASH"].append({"content": "All selected custom configs could not be found or are not UI/API custom configs.", "type": "error"})
            DATA.update({"RELOADING": False, "CONFIG_CHANGED": False})
            return

        for method, configs_for_method in remaining_configs_by_method.items():
            try:
                API_CLIENT.bulk_save_configs(configs_for_method, method)
            except Exception as e:
                DATA["TO_FLASH"].append({"content": f"An error occurred while saving the custom configs: {e}", "type": "error"})
                DATA.update({"RELOADING": False, "CONFIG_CHANGED": False})
                return
        DATA["TO_FLASH"].append({"content": f"Deleted config{'s' if len(configs_to_delete) > 1 else ''}: {', '.join(configs_to_delete)}", "type": "success"})
        DATA["RELOADING"] = False

    DATA.update({"RELOADING": True, "LAST_RELOAD": time(), "CONFIG_CHANGED": True})
    CONFIG_TASKS_EXECUTOR.submit(delete_configs, configs)

    return redirect(
        url_for(
            "loading",
            next=url_for("configs.configs_page"),
            message=f"Deleting selected config{'s' if len(configs) > 1 else ''}",
        )
    )


@configs.route("/configs/new", methods=["GET", "POST"])
@login_required
def configs_new():
    if request.method == "POST":
        if API_CLIENT.readonly:
            return handle_error("Database is in read-only mode", "configs")

        verify_data_in_form(
            data={"service": None},
            err_message="Missing service parameter on /configs/new.",
            redirect_url="configs.configs_new",
            next=True,
        )
        service = request.form["service"]
        services = BW_CONFIG.get_config(global_only=True, with_drafts=True, methods=False, filtered_settings=("SERVER_NAME",))["SERVER_NAME"].split()
        if service != "global" and service not in services:
            return handle_error(f"Service {service} does not exist.", "configs.configs_new", True)

        verify_data_in_form(
            data={"type": None},
            err_message="Missing type parameter on /configs/new.",
            redirect_url="configs.configs_new",
            next=True,
        )
        config_type = request.form["type"]
        if config_type not in CONFIG_TYPES:
            return handle_error("Invalid type parameter on /configs/new.", "configs.configs_new", True)

        verify_data_in_form(
            data={"name": None},
            err_message="Missing name parameter on /configs/new.",
            redirect_url="configs.configs_new",
            next=True,
        )
        config_name = request.form["name"]
        if not match(r"^[\w_-]{1,255}$", config_name):
            return handle_error("Invalid name parameter on /configs/new.", "configs.configs_new", True)

        verify_data_in_form(
            data={"value": None},
            err_message="Missing value parameter on /configs/new.",
            redirect_url="configs.configs_new",
            next=True,
        )
        config_value = request.form["value"].replace("\r\n", "\n").strip()
        is_draft = request.form.get("is_draft", "no") == "yes"
        DATA.load_from_file()

        def create_config(
            service: Optional[str],
            config_type: Literal[
                "HTTP",
                "SERVER_HTTP",
                "DEFAULT_SERVER_HTTP",
                "MODSEC_CRS",
                "MODSEC",
                "STREAM",
                "SERVER_STREAM",
                "DEFAULT_SERVER_STREAM",
                "CRS_PLUGINS_BEFORE",
                "CRS_PLUGINS_AFTER",
            ],
            config_name: str,
            config_value: str,
            is_draft: bool,
        ):
            wait_applying()
            config_type = config_type.lower()

            try:
                API_CLIENT.create_config(
                    service=service,
                    type=config_type,
                    name=config_name,
                    data=config_value,
                    is_draft=is_draft,
                )
            except Exception as e:
                error_msg = str(e)
                if "already exists" in error_msg:
                    DATA["TO_FLASH"].append(
                        {
                            "content": f"Config {config_type}/{config_name}{' for service ' + service if service else ''} already exists",
                            "type": "error",
                        }
                    )
                    DATA.update({"RELOADING": False, "CONFIG_CHANGED": False})
                    return
                DATA["TO_FLASH"].append({"content": f"An error occurred while saving the custom configs: {error_msg}", "type": "error"})
                return
            DATA["TO_FLASH"].append(
                {
                    "content": f"Created custom configuration {config_type}/{config_name}{' for service ' + service if service else ''}",
                    "type": "success",
                }
            )
            DATA["RELOADING"] = False

        DATA.update({"RELOADING": True, "LAST_RELOAD": time(), "CONFIG_CHANGED": True})
        CONFIG_TASKS_EXECUTOR.submit(create_config, service if service != "global" else None, config_type, config_name, config_value, is_draft)

        return redirect(
            url_for(
                "loading",
                next=url_for("configs.configs_edit", service="global" if service == "global" else service, config_type=config_type.lower(), name=config_name),
                message=f"Creating custom configuration {config_type}/{config_name}{' for service' + service if service != 'global' else ''}",
            )
        )

    clone = request.args.get("clone", "")
    config_service = ""
    config_type = ""
    config_name = ""
    is_draft = "no"

    if clone:
        config_service, config_type, config_name = clone.split("/")
        db_custom_config = API_CLIENT.get_config_item(config_service if config_service != "global" else None, config_type, config_name, with_data=True)
        clone = db_custom_config.get("data", "")
        is_draft = "yes" if db_custom_config.get("is_draft") else "no"

    return render_template(
        "config_edit.html",
        config_types=CONFIG_TYPES,
        config_value=clone,
        config_service=config_service,
        type=config_type.upper(),
        name=config_name,
        is_draft=is_draft,
        use_modsecurity_global_crs=_use_modsecurity_global_crs(),
        global_crs_service_scoped_modsec_crs_error=DB.GLOBAL_CRS_SERVICE_SCOPED_MODSEC_CRS_ERROR,
        services=BW_CONFIG.get_config(global_only=True, methods=False, with_drafts=True, filtered_settings=("SERVER_NAME",))["SERVER_NAME"].split(),
    )


@configs.route("/configs/<string:service>/<string:config_type>/<string:name>", methods=["GET", "POST"])
@login_required
def configs_edit(service: str, config_type: str, name: str):
    if service == "global":
        service = None
    name = secure_filename(name)

    db_config = API_CLIENT.get_config_item(service, config_type, name, with_data=True)
    if not db_config:
        return handle_error(f"Config {config_type}/{name}{' for service ' + service if service else ''} does not exist.", "configs", True)
    is_draft = "yes" if db_config.get("is_draft") else "no"

    if request.method == "POST":
        if API_CLIENT.readonly:
            return handle_error("Database is in read-only mode", "configs")

        if not db_config["template"] and not is_editable_method(db_config["method"]):
            return handle_error(
                f"Config {config_type}/{name}{' for service ' + service if service else ''} is not a UI/API custom config and cannot be edited.",
                "configs",
                True,
            )

        verify_data_in_form(
            data={"service": None},
            err_message="Missing service parameter on /configs/new.",
            redirect_url="configs.configs_new",
            next=True,
        )
        new_service = request.form["service"]
        services = BW_CONFIG.get_config(global_only=True, with_drafts=True, methods=False, filtered_settings=("SERVER_NAME",))["SERVER_NAME"].split()
        if new_service != "global" and new_service not in services:
            return handle_error(f"Service {new_service} does not exist.", "configs.configs_new", True)

        if new_service == "global":
            new_service = None

        verify_data_in_form(
            data={"type": None},
            err_message="Missing type parameter on /configs/new.",
            redirect_url="configs.configs_new",
            next=True,
        )
        new_type = request.form["type"]
        if new_type not in CONFIG_TYPES:
            return handle_error("Invalid type parameter on /configs/new.", "configs.configs_new", True)
        new_type = new_type.lower()

        verify_data_in_form(
            data={"name": None},
            err_message="Missing name parameter on /configs/new.",
            redirect_url="configs.configs_new",
            next=True,
        )
        new_name = secure_filename(request.form["name"])
        if not match(r"^[\w_-]{1,255}$", new_name):
            return handle_error("Invalid name parameter on /configs/new.", "configs.configs_new", True)

        # Forbid renaming template-based configs (content can still be edited)
        if db_config.get("template") and new_name != name:
            return handle_error(
                "Renaming a template-based custom config is not allowed.",
                "configs",
                True,
            )

        verify_data_in_form(
            data={"value": None},
            err_message="Missing value parameter on /configs/new.",
            redirect_url="configs.configs_new",
            next=True,
        )
        config_value = request.form["value"].replace("\r\n", "\n").strip()
        new_is_draft = request.form.get("is_draft", "no") == "yes"
        DATA.load_from_file()

        db_config_service = db_config.get("service") or None
        db_config_service = None if db_config_service in ("global", "") else db_config_service
        no_changes = (
            db_config["type"] == new_type
            and db_config["name"] == new_name
            and db_config_service == new_service
            and db_config.get("data", "") == config_value
            and db_config.get("is_draft", False) == new_is_draft
        )
        if no_changes:
            return handle_error("No values were changed.", "configs", True)

        try:
            API_CLIENT.update_config(
                service,
                config_type,
                name,
                body={"service": new_service, "type": new_type, "name": new_name, "data": config_value, "is_draft": new_is_draft},
            )
            flash(f"Saved custom configuration {new_type}/{new_name}{' for service ' + new_service if new_service else ''}")
        except Exception as e:
            flash(f"An error occurred while saving the custom configs: {e}", "error")

        return redirect(
            url_for(
                "configs.configs_edit",
                service=new_service or "global",
                config_type=new_type,
                name=new_name,
            )
        )

    return render_template(
        "config_edit.html",
        config_types=CONFIG_TYPES,
        config_value=db_config.get("data", ""),
        config_service=db_config.get("service") if db_config.get("service") not in ("global", "") else None,
        type=db_config["type"].upper(),
        name=db_config["name"],
        config_method=db_config["method"],
        config_template=db_config.get("template"),
        is_draft=is_draft,
        use_modsecurity_global_crs=_use_modsecurity_global_crs(),
        global_crs_service_scoped_modsec_crs_error=DB.GLOBAL_CRS_SERVICE_SCOPED_MODSEC_CRS_ERROR,
        services=BW_CONFIG.get_config(global_only=True, methods=False, with_drafts=True, filtered_settings=("SERVER_NAME",))["SERVER_NAME"].split(),
    )
