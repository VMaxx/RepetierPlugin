from UM.i18n import i18nCatalog
from UM.Logger import Logger
from UM.Signal import signalemitter
from UM.Message import Message
from UM.Util import parseBool

from cura.CuraApplication import CuraApplication

from cura.PrinterOutputDevice import PrinterOutputDevice, ConnectionState
from cura.PrinterOutput.NetworkedPrinterOutputDevice import NetworkedPrinterOutputDevice
from cura.PrinterOutput.PrinterOutputModel import PrinterOutputModel
from cura.PrinterOutput.PrintJobOutputModel import PrintJobOutputModel
from cura.PrinterOutput.NetworkCamera import NetworkCamera

from cura.PrinterOutput.GenericOutputController import GenericOutputController

from PyQt5.QtNetwork import QHttpMultiPart, QHttpPart, QNetworkRequest, QNetworkAccessManager, QNetworkReply
from PyQt5.QtCore import QUrl, QTimer, pyqtSignal, pyqtProperty, pyqtSlot, QCoreApplication
from PyQt5.QtGui import QImage, QDesktopServices

import json
import os.path
import re
import datetime
from time import time
import base64

from typing import Any, Callable, Dict, List, Optional, Union
from UM.Scene.SceneNode import SceneNode #For typing.
from UM.FileHandler.FileHandler import FileHandler #For typing.

i18n_catalog = i18nCatalog("cura")


##  Repetier connected (wifi / lan) printer using the Repetier API
@signalemitter
class RepetierOutputDevice(NetworkedPrinterOutputDevice):
    def __init__(self, instance_id: str, address: str, port: int, properties: dict, parent = None) -> None:
        super().__init__(device_id = instance_id, address = address, properties = properties, parent = parent)

        self._address = address
        self._port = port
        self._path = properties.get(b"path", b"/").decode("utf-8")
        if self._path[-1:] != "/":
            self._path += "/"
        self._id = instance_id
        self._properties = properties  # Properties dict as provided by zero conf

        self._gcode = [] # type: List[str]
        self._auto_print = True
        self._forced_queue = False

        # We start with a single extruder, but update this when we get data from Repetier
        self._number_of_extruders_set = False
        self._number_of_extruders = 1

        # Try to get version information from plugin.json
        plugin_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin.json")
        try:
            with open(plugin_file_path) as plugin_file:
                plugin_info = json.load(plugin_file)
                plugin_version = plugin_info["version"]
        except:
            # The actual version info is not critical to have so we can continue
            plugin_version = "Unknown"
            Logger.logException("w", "Could not get version information for the plugin")

        self._user_agent_header = "User-Agent".encode()
        self._user_agent = ("%s/%s %s/%s" % (
            CuraApplication.getInstance().getApplicationName(),
            CuraApplication.getInstance().getVersion(),
            "RepetierPlugin",
            CuraApplication.getInstance().getVersion()
        ))

        #base_url + "printer/api/" + self._key +
        
        self._api_prefix = "printer/api/" + self._id.replace("'", "").replace(" ","_")
        self._job_prefix = "printer/job/" + self._id.replace("'", "").replace(" ","_")
        self._save_prefix = "printer/model/" + self._id.replace("'", "").replace(" ","_")
        self._api_header = "x-api-key".encode()
        self._api_key = b""

        self._protocol = "https" if properties.get(b'useHttps') == b"true" else "http"
        self._base_url = "%s://%s:%d%s" % (self._protocol, self._address, self._port, self._path)
        self._api_url = self._base_url + self._api_prefix
        self._job_url = self._base_url + self._job_prefix
        self._save_url = self._base_url + self._save_prefix

        self._basic_auth_header = "Authorization".encode()
        self._basic_auth_data = None
        basic_auth_username = properties.get(b"userName", b"").decode("utf-8")
        basic_auth_password = properties.get(b"password", b"").decode("utf-8")
        if basic_auth_username and basic_auth_password:
            data = base64.b64encode(("%s:%s" % (basic_auth_username, basic_auth_password)).encode()).decode("utf-8")
            self._basic_auth_data = ("basic %s" % data).encode()

        self._monitor_view_qml_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "MonitorItem.qml")

        name = self._id
        matches = re.search(r"^\"(.*)\"\._octoprint\._tcp.local$", name)
        if matches:
            name = matches.group(1)
        #Logger.log("d", "NAME IS: %s", name)
        #Logger.log("d", "ADDRESS IS: %s", self._address)
        self.setPriority(2) # Make sure the output device gets selected above local file output
        self.setName(name)
        self.setShortDescription(i18n_catalog.i18nc("@action:button", "Print with Repetier"))
        self.setDescription(i18n_catalog.i18nc("@properties:tooltip", "Print with Repetier"))
        self.setIconName("print")
        self.setConnectionText(i18n_catalog.i18nc("@info:status", "Connected to Repetier on {0}").format(self._id.replace("'", "").replace(" ","_")))

        self._post_reply = None

        self._progress_message = None # type: Union[None, Message]
        self._error_message = None # type: Union[None, Message]
        self._connection_message = None # type: Union[None, Message]

        self._queued_gcode_commands = [] # type: List[str]
        self._queued_gcode_timer = QTimer()
        self._queued_gcode_timer.setInterval(0)
        self._queued_gcode_timer.setSingleShot(True)
        self._queued_gcode_timer.timeout.connect(self._sendQueuedGcode)

        self._update_timer = QTimer()
        self._update_timer.setInterval(2000)  # TODO; Add preference for update interval
        self._update_timer.setSingleShot(False)
        self._update_timer.timeout.connect(self._update)

        self._camera_mirror = False
        self._camera_rotation = 0
        self._camera_url = ""
        self._camera_shares_proxy = False

        self._sd_supported = False

        self._plugin_data = {} #type: Dict[str, Any]

        self._output_controller = GenericOutputController(self)
        
    def getProperties(self) -> Dict[bytes, bytes]:
        return self._properties

    @pyqtSlot(str, result = str)
    def getProperty(self, key: str) -> str:
        key_b = key.encode("utf-8")
        if key_b in self._properties:
            return self._properties.get(key_b, b"").decode("utf-8")
        else:
            return ""

    ##  Get the unique key of this machine
    #   \return key String containing the key of the machine.
    @pyqtSlot(result = str)
    def getId(self) -> str:
        return self._id

    ##  Set the API key of this Repetier instance
    def setApiKey(self, api_key: str) -> None:
        self._api_key = api_key.encode()

    ##  Name of the instance (as returned from the zeroConf properties)
    @pyqtProperty(str, constant = True)
    def name(self) -> str:
        return self._name

    ##  Version (as returned from the zeroConf properties)
    @pyqtProperty(str, constant=True)
    def repetierVersion(self) -> str:
        return self._properties.get(b"version", b"").decode("utf-8")

    ## IPadress of this instance
    @pyqtProperty(str, constant=True)
    def ipAddress(self) -> str:
        return self._address

    ## IP address of this instance
    #  Overridden from NetworkedPrinterOutputDevice because OctoPrint does not
    #  send the ip address with zeroconf
    @pyqtProperty(str, constant=True)
    def address(self) -> str:
        return self._address

    ## port of this instance
    @pyqtProperty(int, constant=True)
    def port(self) -> int:
        return self._port

    ## path of this instance
    @pyqtProperty(str, constant=True)
    def path(self) -> str:
        return self._path

    ## absolute url of this instance
    @pyqtProperty(str, constant=True)
    def baseURL(self) -> str:
        return self._base_url

    cameraOrientationChanged = pyqtSignal()

    @pyqtProperty("QVariantMap", notify = cameraOrientationChanged)
    def cameraOrientation(self) -> Dict[str, Any]:
        return {
            "mirror": self._camera_mirror,
            "rotation": self._camera_rotation,
        }

    def _update(self) -> None:
        ## Request 'general' printer data
        self.get("stateList", self._onRequestFinished)
        ## Request print_job data
        self.get("listPrinter", self._onRequestFinished)
        ## Request print_job data
        ##self.get("getPrinterConfig", self._onRequestFinished)

    def _createEmptyRequest(self, target: str, content_type: Optional[str] = "application/json") -> QNetworkRequest:
        if "upload" in target:
             if self._forced_queue or not self._auto_print:
                  request = QNetworkRequest(QUrl(self._save_url + "?a=" + target))
             else:
                  request = QNetworkRequest(QUrl(self._job_url + "?a=" + target))
        else:
             request = QNetworkRequest(QUrl(self._api_url + "?a=" + target))
        request.setRawHeader(self._user_agent_header, self._user_agent.encode())
        request.setRawHeader(self._api_header, self._api_key)
        if content_type is not None:
            request.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
        if self._basic_auth_data:
            request.setRawHeader(self._basic_auth_header, self._basic_auth_data)
        return request

    def close(self) -> None:
        self.setConnectionState(ConnectionState.closed)
        if self._progress_message:
            self._progress_message.hide()
        if self._error_message:
            self._error_message.hide()
        self._update_timer.stop()

    def requestWrite(self, nodes: List[SceneNode], file_name: Optional[str] = None, limit_mimetypes: bool = False, file_handler: Optional[FileHandler] = None, **kwargs: str) -> None:
        self.writeStarted.emit(self)

        active_build_plate = CuraApplication.getInstance().getMultiBuildPlateModel().activeBuildPlate
        scene = CuraApplication.getInstance().getController().getScene()
        gcode_dict = getattr(scene, "gcode_dict", None)
        if not gcode_dict:
            return
        self._gcode = gcode_dict.get(active_build_plate, None)

        self.startPrint()

    ##  Start requesting data from the instance
    def connect(self) -> None:
        self._createNetworkManager()

        self.setConnectionState(ConnectionState.connecting)
        self._update()  # Manually trigger the first update, as we don't want to wait a few secs before it starts.
        Logger.log("d", "Connection with instance %s with url %s started", self._id.replace("'", "").replace(" ","_"), self._base_url)
        self._update_timer.start()

        self._last_response_time = None
        self._setAcceptsCommands(False)
        self.setConnectionText(i18n_catalog.i18nc("@info:status", "Connecting to Repetier on {0}").format(self._base_url))

        ## Request 'settings' dump
        self.get("getPrinterConfig", self._onRequestFinished)
        self._settings_reply = self._manager.get(self._createEmptyRequest("getPrinterConfig"))
        self._settings_reply = self._manager.get(self._createEmptyRequest("stateList"))

    ##  Stop requesting data from the instance
    def disconnect(self) -> None:
        Logger.log("d", "Connection with instance %s with url %s stopped", self._id.replace("'", "").replace(" ","_"), self._base_url)
        self.close()

    def pausePrint(self) -> None:
        if not self._printers[0].activePrintJob:
            return
        #Logger.log("d", "Pause attempted: %s ", self._printers[0].activePrintJob.state)
        self._sendJobCommand("pause")

    def resumePrint(self) -> None:
        if not self._printers[0].activePrintJob:
            return
        #Logger.log("d", "Resume attempted: %s ", self._printers[0].activePrintJob.state)
        if self._printers[0].activePrintJob.state == "paused":
            self._sendJobCommand("start")
        else:
            self._sendJobCommand("pause")

    def cancelPrint(self) -> None:
        self._sendJobCommand("cancel")

    def startPrint(self) -> None:
        global_container_stack = CuraApplication.getInstance().getGlobalContainerStack()
        if not global_container_stack:
            return

        if self._error_message:
            self._error_message.hide()
            self._error_message = None

        if self._progress_message:
            self._progress_message.hide()
            self._progress_message = None

        self._auto_print = parseBool(global_container_stack.getMetaDataEntry("repetier_auto_print", True))
        self._forced_queue = False

        if self.activePrinter.state not in ["idle", ""]:
            Logger.log("d", "Tried starting a print, but current state is %s" % self.activePrinter.state)
            if not self._auto_print:
                # allow queueing the job even if Repetier is currently busy if autoprinting is disabled
                self._error_message = None
            elif self.activePrinter.state == "offline":
                self._error_message = Message(i18n_catalog.i18nc("@info:status", "The printer is offline. Unable to start a new job."))
            else:
                self._error_message = Message(i18n_catalog.i18nc("@info:status", "Repetier is busy. Unable to start a new job."))

            if self._error_message:
                self._error_message.addAction("Queue", i18n_catalog.i18nc("@action:button", "Queue job"), None, i18n_catalog.i18nc("@action:tooltip", "Queue this print job so it can be printed later"))
                self._error_message.actionTriggered.connect(self._queuePrint)
                self._error_message.show()
                return

        self._startPrint()

    def _queuePrint(self, message_id: Optional[str] = None, action_id: Optional[str] = None) -> None:
        if self._error_message:
            self._error_message.hide()
        self._forced_queue = True
        self._startPrint()
        
    def _startPrint(self) -> None:
        global_container_stack = CuraApplication.getInstance().getGlobalContainerStack()
        if not global_container_stack:
            return

        if self._auto_print and not self._forced_queue:
            CuraApplication.getInstance().getController().setActiveStage("MonitorStage")

            # cancel any ongoing preheat timer before starting a print
            try:
                self._printers[0].stopPreheatTimers()
            except AttributeError:
                # stopPreheatTimers was added after Cura 3.3 beta
                pass

        self._progress_message = Message(i18n_catalog.i18nc("@info:status", "Sending data to Repetier"), 0, False, -1)
        self._progress_message.addAction("Cancel", i18n_catalog.i18nc("@action:button", "Cancel"), None, "")
        self._progress_message.actionTriggered.connect(self._cancelSendGcode)
        self._progress_message.show()

        ## Mash the data into single string
        single_string_file_data = ""
        last_process_events = time()
        for line in self._gcode:
            single_string_file_data += line
            if time() > last_process_events + 0.05:
                # Ensure that the GUI keeps updated at least 20 times per second.
                QCoreApplication.processEvents()
                last_process_events = time()

        job_name = CuraApplication.getInstance().getPrintInformation().jobName.strip()
        Logger.log("d", "Print job: [%s]", job_name)
        if job_name is "":
            job_name = "untitled_print"
        file_name = "%s.gcode" % job_name

        ##  Create multi_part request
        post_parts = [] # type: List[QHttpPart]

            ##  Create parts (to be placed inside multipart)
        post_part = QHttpPart()
        post_part.setHeader(QNetworkRequest.ContentDispositionHeader, "form-data; name=\"a\"")
        post_part.setBody(b"upload")
        post_parts.append(post_part)

        if self._auto_print and not self._forced_queue:
            post_part = QHttpPart()
            post_part.setHeader(QNetworkRequest.ContentDispositionHeader, "form-data; name=\"%s\"" % file_name)
            post_part.setBody(b"upload")
            post_parts.append(post_part)
            
        post_part = QHttpPart()
        post_part.setHeader(QNetworkRequest.ContentDispositionHeader, "form-data; name=\"file\"; filename=\"%s\"" % file_name)
        post_part.setBody(single_string_file_data.encode())
        post_parts.append(post_part)

        destination = "local"
        if self._sd_supported and parseBool(global_container_stack.getMetaDataEntry("Repetier_store_sd", False)):
            destination = "sdcard"

        try:
            ##  Post request + data
            ##post_request = self._createApiRequest("files/" + destination)
            post_request = self._createEmptyRequest("upload&name=%s" % file_name)
            self._post_reply = self.postFormWithParts("upload&name=%s" % file_name, post_parts, on_finished=self._onRequestFinished, on_progress=self._onUploadProgress)
            ##self._post_reply = self._manager.post(post_request, self._post_multi_part)
            ##self._post_reply.uploadProgress.connect(self._onUploadProgress)

        except IOError:
            self._progress_message.hide()
            self._error_message = Message(i18n_catalog.i18nc("@info:status", "Unable to send data to Repetier."))
            self._error_message.show()
        except Exception as e:
            self._progress_message.hide()
            Logger.log("e", "An exception occurred in network connection: %s" % str(e))

        self._gcode = []

    def _cancelSendGcode(self, message_id: Optional[str] = None, action_id: Optional[str] = None) -> None:
        if self._post_reply:
            Logger.log("d", "Stopping upload because the user pressed cancel.")
            try:
                self._post_reply.uploadProgress.disconnect(self._onUploadProgress)
            except TypeError:
                pass  # The disconnection can fail on mac in some cases. Ignore that.

            self._post_reply.abort()
            self._post_reply = None
        if self._progress_message:
            self._progress_message.hide()

    def sendCommand(self, command: str) -> None:
        self._queued_gcode_commands.append(command)
        self._queued_gcode_timer.start()

    # Send gcode commands that are queued in quick succession as a single batch
    def _sendQueuedGcode(self) -> None:
        if self._queued_gcode_commands:
            self._sendCommandToApi("send", "&data={\"cmd\":\"" + self._queued_gcode_commands + "\"}")
            #Logger.log("d", "Sent gcode command to Repetier instance: %s", self._queued_gcode_commands)
            self._queued_gcode_commands = []

    def _sendJobCommand(self, command: str) -> None:
        #Logger.log("d", "sendJobCommand: %s", command)
        if (command=="pause"):
            self._sendCommandToApi("send", "&data={\"cmd\":\"@pause\"}")
        if (command=="start"):
            self._manager.get(self._createEmptyRequest("continueJob"))
        if (command=="cancel"):
            self._manager.get(self._createEmptyRequest("stopJob"))
        #Logger.log("d", "Sent job command to Repetier instance: %s %s" % (command,self.jobState))

    def _sendCommandToApi(self, end_point, commands):	
        command_request = QNetworkRequest(QUrl(self._api_url + "?a=" + end_point))
        command_request.setRawHeader(self._user_agent_header, self._user_agent.encode())
        command_request.setRawHeader(self._api_header, self._api_key)
        if self._basic_auth_data:
            command_request.setRawHeader(self._basic_auth_header, self._basic_auth_data)	        
        command_request.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
        if isinstance(commands, list):
            data = json.dumps({"commands": commands})
        else:
            data = commands
        #Logger.log("d", "_sendCommandToAPI: %s", data)
        self._command_reply = self._manager.post(command_request, data.encode())


        ##  Handler for all requests that have finished.
    def _onRequestFinished(self, reply: QNetworkReply) -> None:
        global_container_stack = CuraApplication.getInstance().getGlobalContainerStack()
        if not global_container_stack:
            return
        if reply.error() == QNetworkReply.TimeoutError:
            Logger.log("w", "Received a timeout on a request to the instance")
            self._connection_state_before_timeout = self._connection_state
            self.setConnectionState(ConnectionState.error)
            return

        if self._connection_state_before_timeout and reply.error() == QNetworkReply.NoError:  #  There was a timeout, but we got a correct answer again.
            if self._last_response_time:
                Logger.log("d", "We got a response from the instance after %s of silence", time() - self._last_response_time)
            self.setConnectionState(self._connection_state_before_timeout)
            self._connection_state_before_timeout = None

        if reply.error() == QNetworkReply.NoError:
            self._last_response_time = time()

        http_status_code = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        if not http_status_code:
            # Received no or empty reply
            return

        error_handled = False
        if reply.operation() == QNetworkAccessManager.GetOperation:
            #Logger.log("d", "reply.url() = %s", reply.url().toString())
            if self._api_prefix + "?a=stateList" in reply.url().toString():  # Status update from /printer.
                if not self._printers:
                    self._createPrinterList()

                printer = self._printers[0]

                if http_status_code == 200:
                    if not self.acceptsCommands:
                        self._setAcceptsCommands(True)
                        self.setConnectionText(i18n_catalog.i18nc("@info:status", "Connected to Repetier on {0}").format(self._id.replace("'", "").replace(" ","_")))

                    if self._connection_state == ConnectionState.connecting:
                        self.setConnectionState(ConnectionState.connected)
                    try:
                        json_data = json.loads(bytes(reply.readAll()).decode("utf-8"))
                    except json.decoder.JSONDecodeError:
                        Logger.log("w", "Received invalid JSON from Repetier instance.")
                        json_data = {}
                    #if "temperature" in json_data:
                    try:
                        if "numExtruder" in json_data[self._id.replace("'", "").replace(" ","_")]:
                            self._number_of_extruders = 0
                            printer_state = "idle"
                            #while "tool%d" % self._num_extruders in json_data["temperature"]:
                            self._number_of_extruders=json_data[self._id.replace("'", "").replace(" ","_")]["numExtruder"]
                            if self._number_of_extruders > 1:
                                # Recreate list of printers to match the new _number_of_extruders
                                self._createPrinterList()
                                printer = self._printers[0]

                            if self._number_of_extruders > 0:
                                self._number_of_extruders_set = True
      
                            # Check for hotend temperatures
                            for index in range(0, self._number_of_extruders):
                                extruder = printer.extruders[index]
                                if "extruder" in json_data[self._id.replace("'", "").replace(" ","_")]:                            
                                    hotend_temperatures = json_data[self._id.replace("'", "").replace(" ","_")]["extruder"]
                                    #Logger.log("d", "target end temp %s", hotend_temperatures[index]["tempSet"])
                                    #Logger.log("d", "target end temp %s", hotend_temperatures[index]["tempRead"])
                                    extruder.updateTargetHotendTemperature(hotend_temperatures[index]["tempSet"])
                                    extruder.updateHotendTemperature(hotend_temperatures[index]["tempRead"])                                    
                                else:
                                    extruder.updateTargetHotendTemperature(0)
                                    extruder.updateHotendTemperature(0)
                        #Logger.log("d", "json_data %s", json_data[self._key])
                        if "heatedBed" in json_data[self._id.replace("'", "").replace(" ","_")]:
                            bed_temperatures = json_data[self._id.replace("'", "").replace(" ","_")]["heatedBed"]
                            actual_temperature = bed_temperatures["tempRead"] if bed_temperatures["tempRead"] is not None else -1
                            printer.updateBedTemperature(actual_temperature)
                            target_temperature = bed_temperatures["tempSet"] if bed_temperatures["tempSet"] is not None else -1                                    
                            printer.updateTargetBedTemperature(target_temperature)
                            #Logger.log("d", "target bed temp %s", target_temperature)
                            #Logger.log("d", "actual bed temp %s", actual_temperature)
                        else:
                            if "heatedBeds" in json_data[self._id.replace("'", "").replace(" ","_")]:
                                bed_temperatures = json_data[self._id.replace("'", "").replace(" ","_")]["heatedBeds"][0]
                                actual_temperature = bed_temperatures["tempRead"] if bed_temperatures["tempRead"] is not None else -1
                                printer.updateBedTemperature(actual_temperature)
                                target_temperature = bed_temperatures["tempSet"] if bed_temperatures["tempSet"] is not None else -1                                    
                                printer.updateTargetBedTemperature(target_temperature)
                                #Logger.log("d", "target bed temp %s", target_temperature)
                                #Logger.log("d", "actual bed temp %s", actual_temperature)
                            else:
                                printer.updateBedTemperature(-1)
                                printer.updateTargetBedTemperature(0)
                                printer.updateState(printer_state)
                    except:
                        Logger.log("w", "Received invalid JSON from Repetier instance.")                    
                        json_data = {}
                        printer.activePrintJob.updateState("offline")
                        self.setConnectionText(i18n_catalog.i18nc("@info:status", "Repetier on {0} configuration is invalid").format(self._id.replace("'", "").replace(" ","_")))

                elif http_status_code == 401:
                    printer.updateState("offline")
                    if printer.activePrintJob:
                        printer.activePrintJob.updateState("offline")
                    self.setConnectionText(i18n_catalog.i18nc("@info:status", "Repetier on {0} does not allow access to print").format(self._id.replace("'", "").replace(" ","_")))
                    error_handled = True
                elif http_status_code == 409:
                    if self._connection_state == ConnectionState.connecting:
                        self.setConnectionState(ConnectionState.connected)

                    printer.updateState("offline")
                    if printer.activePrintJob:
                        printer.activePrintJob.updateState("offline")
                    self.setConnectionText(i18n_catalog.i18nc("@info:status", "The printer connected to Repetier on {0} is not operational").format(self._id.replace("'", "").replace(" ","_")))
                    error_handled = True
                else:
                    printer.updateState("offline")
                    if printer.activePrintJob:
                        printer.activePrintJob.updateState("offline")
                    Logger.log("w", "Received an unexpected returncode: %d", http_status_code)

            elif self._api_prefix + "?a=listPrinter" in reply.url().toString():  # Status update from /job:
                if not self._printers:
                    self._createPrinterList()
                printer = self._printers[0]
                if http_status_code == 200:
                    try:
                        json_data = json.loads(bytes(reply.readAll()).decode("utf-8"))
                    except json.decoder.JSONDecodeError:
                        Logger.log("w", "Received invalid JSON from Repetier instance.")
                        json_data = {}

                    #try:
                    if printer:
                        print_job_state = "ready"
                        printer.updateState("idle")
                        if printer.activePrintJob is None:
                            print_job = PrintJobOutputModel(output_controller=self._output_controller)
                            printer.updateActivePrintJob(print_job)
                        else:
                            print_job = printer.activePrintJob
                            #job_state = "ready"
                            #if "job" in json_data[0]:
                            #    if json_data[0]["job"] != "none":
                            #        job_state = "printing"

                        print_job_state = "ready"
                        if "job" in json_data[0]:
                            #Logger.log("d","Jobname: %s",json_data[0]["job"])
                            if json_data[0]["job"] != "none":
                                print_job.updateName(json_data[0]["job"])
                                print_job_state = "printing"
                            if json_data[0]["job"] == "none":                                
                                print_job_state = "ready"
                                printer.updateState("idle")
                                print_job = PrintJobOutputModel(output_controller=self._output_controller)
                                printer.updateActivePrintJob(print_job)
                        if "paused" in json_data[0]:
                            if json_data[0]["paused"] != False:
                                print_job_state = "paused"								
                        #printer.updateState(printer_state)
                        #if "state" in json_data:
                        #    if json_data["state"]["flags"]["error"]:
                        #        job_state = "error"
                        #    elif json_data["state"]["flags"]["paused"]:
                        #        job_state = "paused"
                        #    elif json_data["state"]["flags"]["printing"]:
                        #        job_state = "printing"
                        #    elif json_data["state"]["flags"]["ready"]:
                        #        job_state = "ready"
                        print_job.updateState(print_job_state)
                        
                        #progress = json_data["progress"]["completion"]
                        if "done" in json_data[0]:
                            progress = json_data[0]["done"]
                        if "start" in json_data[0]:
                            if json_data[0]["start"]:
                                ##self.setTimeElapsed(json_data[0]["start"])
                                ##self.setTimeElapsed(datetime.datetime.fromtimestamp(json_data[0]["start"]).strftime('%Y-%m-%d %H:%M:%S'))
                                if json_data[0]["printTime"]:
                                    print_job.updateTimeTotal(json_data[0]["printTime"])
                                if json_data[0]["printedTimeComp"]:
                                    print_job.updateTimeElapsed(json_data[0]["printedTimeComp"])
                                elif progress > 0:
                                    print_job.updateTimeTotal(json_data[0]["printTime"] * (progress / 100))
                                else:
                                    print_job.updateTimeTotal(0)
                            else:
                                print_job.updateTimeElapsed(0)
                                print_job.updateTimeTotal(0)
                            print_job.updateName(json_data[0]["job"])
                    #except:
                    #    if printer:
                    #        printer.activePrintJob.updateState("offline")
                    #        self.setConnectionText(i18n_catalog.i18nc("@info:status", "Repetier on {0} configuration is invalid").format(self._key))
                else:
                    if printer:
                        printer.activePrintJob.updateState("offline")
                        self.setConnectionText(i18n_catalog.i18nc("@info:status", "Repetier on {0} bad response").format(self._id.replace("'", "").replace(" ","_")))
            elif self._api_prefix + "?a=getPrinterConfig" in reply.url().toString():  # Repetier settings dump from /settings:                
                if http_status_code == 200:
                    try:
                        json_data = json.loads(bytes(reply.readAll()).decode("utf-8"))
                    except json.decoder.JSONDecodeError:
                        Logger.log("w", "Received invalid JSON from Repetier instance.")
                        json_data = {}

                    if "general" in json_data and "sdcard" in json_data["general"]:
                        self._sd_supported = json_data["general"]["sdcard"]

                    if "webcam" in json_data and "dynamicUrl" in json_data["webcam"]:
                        Logger.log("d", "RepetierOutputDevice: Detected Repetier 89.X")
                        self._camera_shares_proxy = False
                        Logger.log("d", "RepetierOutputDevice: Checking streamurl")                        
                        stream_url = json_data["webcam"]["dynamicUrl"].replace("127.0.0.1",self._address)
                        if not stream_url: #empty string or None
                            self._camera_url = ""
                        elif stream_url[:4].lower() == "http": # absolute uri                        Logger.log("d", "RepetierOutputDevice: stream_url: %s",stream_url)
                            self._camera_url=stream_url
                        elif stream_url[:2] == "//": # protocol-relative
                            self._camera_url = "%s:%s" % (self._protocol, stream_url)
                        elif stream_url[:1] == ":": # domain-relative (on another port)
                            self._camera_url = "%s://%s%s" % (self._protocol, self._address, stream_url)
                        elif stream_url[:1] == "/": # domain-relative (on same port)
                            self._camera_url = "%s://%s:%d%s" % (self._protocol, self._address, self._port, stream_url)
                            self._camera_shares_proxy = True
                        else:
                            Logger.log("w", "Unusable stream url received: %s", stream_url)
                            self._camera_url = ""
                        Logger.log("d", "Set Repetier camera url to %s", self._camera_url)
                        if self._camera_url != "" and len(self._printers) > 0:
                            self._printers[0].setCamera(NetworkCamera(self._camera_url))
                        if parseBool(global_container_stack.getMetaDataEntry("repetier_webcamflip_y", False)):
                            self._camera_rotation = 180
                        self._camera_mirror = False
                        #self.cameraOrientationChanged.emit()
                    if "webcams" in json_data:
                        Logger.log("d", "RepetierOutputDevice: Detected Repetier 90.X")
                        if len(json_data["webcams"])>0:
                            if "dynamicUrl" in json_data["webcams"][0]:
                                self._camera_shares_proxy = False
                                Logger.log("d", "RepetierOutputDevice: Checking streamurl")                        
                                stream_url = json_data["webcams"][0]["dynamicUrl"].replace("127.0.0.1",self._address)
                                if not stream_url: #empty string or None
                                    self._camera_url = ""
                                elif stream_url[:4].lower() == "http": # absolute uri                        Logger.log("d", "RepetierOutputDevice: stream_url: %s",stream_url)
                                    self._camera_url=stream_url
                                elif stream_url[:2] == "//": # protocol-relative
                                    self._camera_url = "%s:%s" % (self._protocol, stream_url)
                                elif stream_url[:1] == ":": # domain-relative (on another port)
                                    self._camera_url = "%s://%s%s" % (self._protocol, self._address, stream_url)
                                elif stream_url[:1] == "/": # domain-relative (on same port)
                                    self._camera_url = "%s://%s:%d%s" % (self._protocol, self._address, self._port, stream_url)
                                    self._camera_shares_proxy = True
                                else:
                                    Logger.log("w", "Unusable stream url received: %s", stream_url)
                                    self._camera_url = ""
                                Logger.log("d", "Set Repetier camera url to %s", self._camera_url)
                                if self._camera_url != "" and len(self._printers) > 0:
                                    self._printers[0].setCamera(NetworkCamera(self._camera_url))
                                if parseBool(global_container_stack.getMetaDataEntry("repetier_webcamflip_y", False)):
                                    self._camera_rotation = 180
                                self._camera_mirror = False
                                self.cameraOrientationChanged.emit()
        elif reply.operation() == QNetworkAccessManager.PostOperation:
            if self._api_prefix + "?a=listModels" in reply.url().toString():  # Result from /files command:
                if http_status_code == 201:
                    Logger.log("d", "Resource created on Repetier instance: %s", reply.header(QNetworkRequest.LocationHeader).toString())
                else:
                    pass  # TODO: Handle errors

                reply.uploadProgress.disconnect(self._onUploadProgress)
                self._progress_message.hide()
                global_container_stack = Application.getInstance().getGlobalContainerStack()
                if self._forced_queue or not self._auto_print:
                    location = reply.header(QNetworkRequest.LocationHeader)
                    if location:
                        file_name = QUrl(reply.header(QNetworkRequest.LocationHeader).toString()).fileName()
                        message = Message(i18n_catalog.i18nc("@info:status", "Saved to Repetier as {0}").format(file_name))
                    else:
                        message = Message(i18n_catalog.i18nc("@info:status", "Saved to Repetier"))
                    message.addAction("open_browser", i18n_catalog.i18nc("@action:button", "Open Repetier..."), "globe",
                                        i18n_catalog.i18nc("@info:tooltip", "Open the Repetier web interface"))
                    message.actionTriggered.connect(self._openRepetierPrint)
                    message.show()

            elif self._api_prefix + "?a=send" in reply.url().toString():  # Result from /job command:
                if http_status_code == 204:
                    Logger.log("d", "Repetier command accepted")
                else:
                    pass  # TODO: Handle errors


        else:
            Logger.log("d", "RepetierOutputDevice got an unhandled operation %s", reply.operation())


    def _onUploadProgress(self, bytes_sent: int, bytes_total: int) -> None:
        if bytes_total > 0:
            # Treat upload progress as response. Uploading can take more than 10 seconds, so if we don't, we can get
            # timeout responses if this happens.
            self._last_response_time = time()

            progress = bytes_sent / bytes_total * 100            
            if progress < 100:
                if progress > self._progress_message.getProgress():
                    self._progress_message.setProgress(progress)
            else:
                self._progress_message.hide()
                self._progress_message = Message(i18n_catalog.i18nc("@info:status", "Storing data on Repetier"), 0, False, -1)
                self._progress_message.show()
        else:
            self._progress_message.setProgress(0)

    def _createPrinterList(self) -> None:
        printer = PrinterOutputModel(output_controller=self._output_controller, number_of_extruders=self._number_of_extruders)
        if self._camera_url != "":
            printer.setCamera(NetworkCamera(self._camera_url))
        printer.updateName(self.name)
        self._printers = [printer]
        self.printersChanged.emit()

    def _openRepetierPrint(self, message_id: Optional[str] = None, action_id: Optional[str] = None) -> None:
        QDesktopServices.openUrl(QUrl(self._base_url))
