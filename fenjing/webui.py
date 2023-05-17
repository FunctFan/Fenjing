from flask import Flask, render_template, request, jsonify

import logging
import threading
import uuid
from urllib.parse import urlparse
from traceback import print_exc
from typing import Callable, List
from functools import partial

from .form import Form
from .form_cracker import FormCracker
from .full_payload_gen import FullPayloadGen
from .scan_url import yield_form
from .requester import Requester
from .const import *
from .colorize import colored

logger = logging.getLogger("webui")
app = Flask(__name__)
tasks = {}

class CallBackLogger:
    def __init__(self, flash_messages, messages):
        self.flash_messages = flash_messages
        self.messages = messages

    def callback_prepare_fullpayloadgen(self, data):
        self.messages.append(
            f"已经分析完毕所有上下文payload。"
        )
        if data["context"]:
            self.messages.append(
                f"以下是在上下文中的值：{', '.join(data['context'].values())}"
            )
        else:
            self.messages.append(
                f"没有上下文payload可以通过waf。。。"
            )
        if not data["will_print"]:
            self.messages.append(
                f"生成的payload将不会具有回显。"
            )
    def callback_generate_fullpayload(self, data):
        self.messages.append(
            f"分析完毕，已为类型{data['gen_type']}生成payload {data['payload']}"
        )
        if not data["will_print"]:
            self.messages.append(
                f"payload将不会产生回显"
            )

    def callback_generate_payload(self, data):
        payload_repr = data['payload']
        if len(payload_repr) > 30:
            payload_repr = payload_repr[:30] + "..."
        self.flash_messages.append(
            "请求{req}对应的payload可以是{payload}".format(
                req = f"{data['gen_type']}({', '.join(repr(arg) for arg in data['args'])})",
                payload = payload_repr
            )
        )

    def callback_submit(self, data):
        self.flash_messages.append(
            f"提交表单{data['form']}的返回值为{data['response'].status_code}"
        )

    def callback_test_form_input(self, data):
        if not data["ok"]:
            return
        testsuccess_msg = "payload测试成功！" if data["test_success"] else "payload测试失败。"
        will_print_msg = "其会产生回显。" if data["will_print"] else "其不会产生回显。"
        self.messages.append(
            testsuccess_msg + will_print_msg
        )

    def __call__(self, callback_type, data):
        default_handler = lambda data: logger.warning(f"{callback_type=} not found")
        return {
            CALLBACK_PREPARE_FULLPAYLOADGEN: self.callback_prepare_fullpayloadgen,
            CALLBACK_GENERATE_FULLPAYLOAD: self.callback_generate_fullpayload,
            CALLBACK_GENERATE_PAYLOAD: self.callback_generate_payload,
            CALLBACK_SUBMIT: self.callback_submit,
            CALLBACK_TEST_FORM_INPUT: self.callback_test_form_input
        }.get(callback_type, default_handler)(data)


class CrackTaskThread(threading.Thread):
    def __init__(self, taskid, url, form):
        super().__init__()
        self.result = None
        self.taskid = taskid
        self.form = form

        self.flash_messages = []
        self.messages = []
        self.callback = CallBackLogger(self.flash_messages, self.messages)
        
        self.cracker = FormCracker(
            url=url,
            form=form,
            requester=Requester(
                interval=0.1,
                user_agent=DEFAULT_USER_AGENT
            ),
            callback=self.callback
        )

    def run(self):
        self.result = self.cracker.crack()
        if self.result:
            self.messages.append(
                f"WAF已绕过"
            )

class InteractiveTaskThread(threading.Thread):
    def __init__(self, taskid, cracker, field, full_payload_gen, cmd):
        super().__init__()
        self.taskid = taskid
        self.cracker = cracker
        self.field = field
        self.full_payload_gen = full_payload_gen
        self.cmd = cmd

        self.flash_messages = []
        self.messages = []
        self.callback = CallBackLogger(self.flash_messages, self.messages)
        
        self.cracker.callback = self.callback

    def run(self):
        payload, will_print = self.full_payload_gen.generate(
            OS_POPEN_READ, 
            self.cmd
        )
        if not will_print:
            self.messages.append(
                f"此payload不会产生回显"
            )
        r = self.cracker.submit({self.field: payload})
        assert r is not None
        self.messages.append(
            f"提交payload的回显如下："
        )
        self.messages.append(r.text)



@app.route("/")
def index():
    return render_template("index.html")

@app.route("/createTask", methods = ["POST", ]) # type: ignore
def create_task():
    if request.form.get("type", None) not in ["crack", "interactive"]:
        logging.info(request.form)
        return jsonify({
            "code": APICODE_WRONG_INPUT,
            "message": f"unknown type {request.form.get('type', None)}"
        })
    task_type = request.form.get("type", None)
    if task_type == "crack":
        url, method, inputs, action = (
            request.form["url"],
            request.form["method"],
            request.form["inputs"],
            request.form["action"],
        )
        form = Form(
            action=action or urlparse(url).path,
            method=method,
            inputs=inputs.split(",")
        )
        taskid = uuid.uuid4().hex
        task = CrackTaskThread(taskid, url, form)
        task.daemon = True
        task.start()
        tasks[taskid] = task
        return jsonify({
            "code": APICODE_OK,
            "taskid": taskid
        })
    elif task_type == "interactive":
        cmd, last_task_id = (
            request.form["cmd"],
            request.form["last_task_id"],
        )
        if last_task_id not in tasks:
            return jsonify({
                "code": APICODE_WRONG_INPUT,
                "message": f"last_task_id not found: {last_task_id}"
            })
        last_task = tasks[last_task_id]
        if not isinstance(last_task, CrackTaskThread):
            return jsonify({
                "code": APICODE_WRONG_INPUT,
                "message": f"last_task_id not found: {last_task_id}"
            })
        if last_task.result is None:
            return jsonify({
                "code": APICODE_WRONG_INPUT,
                "message": f"specified last_task failed: {last_task_id}"
            })
        cracker, field, full_payload_gen = (
            last_task.cracker,
            last_task.result.input_field,
            last_task.result.full_payload_gen
        )
        taskid = uuid.uuid4().hex
        task = InteractiveTaskThread(
            taskid, 
            cracker, 
            field, 
            full_payload_gen, 
            cmd
        )
        task.daemon = True
        task.start()
        tasks[taskid] = task
        return jsonify({
            "code": APICODE_OK,
            "taskid": taskid
        })

@app.route("/watchTask", methods = ["POST", ]) # type: ignore
def watchTask():
    if "taskid" not in request.form:
        return jsonify({
            "code": APICODE_WRONG_INPUT,
            "message": "taskid not provided"
        })
    if request.form["taskid"] not in tasks:
        return jsonify({
            "code": APICODE_WRONG_INPUT,
            "message": f"task not found: {request.form['taskid']}"
        })
    task: CrackTaskThread = tasks[request.form["taskid"]]
    if isinstance(task, CrackTaskThread):
        return jsonify({
            "code": APICODE_OK,
            "taskid": task.taskid,
            "done": not task.is_alive(),
            "messages": task.messages,
            "flash_messages": task.flash_messages,
            "success": task.result.input_field if task.result else None
        })
    elif isinstance(task, InteractiveTaskThread):
        return jsonify({
            "code": APICODE_OK,
            "taskid": task.taskid,
            "done": not task.is_alive(),
            "messages": task.messages,
            "flash_messages": task.flash_messages,
        })

def main():
    app.run()

if __name__ == "__main__":
    main()