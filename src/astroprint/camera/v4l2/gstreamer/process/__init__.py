# coding=utf-8
__author__ = "AstroPrint Product Team <product@astroprint.com>"
__license__ = 'GNU Affero General Public License http://www.gnu.org/licenses/agpl.html'

import logging

from threading import Thread, Event

from .pipelines import pipelineFactory

def startPipelineProcess(device, size, source, encoding, onListeningEvent, reqQ, respQ, debugLevel=0):
	from gi.repository import GObject

	GObject.threads_init()
	mainLoop = GObject.MainLoop()

	logger = logging.getLogger(__name__ + ':processLoop')

	pipeline = pipelineFactory(device, size, source, encoding, mainLoop, debugLevel)
	interface = processInterface(pipeline, reqQ, respQ, mainLoop, onListeningEvent)

	try:
		interface.start()
		logger.info('Pipeline process started')
		mainLoop.run()
		logger.info('Pipeline process ended')

	except KeyboardInterrupt, SystemExit:
		mainLoop.quit()

	except Exception as e:
		mainLoop.quit()
		raise e

	finally:
		if interface.isAlive():
			interface.stop()

		interface.join()

		respQ.close()
		reqQ.close()

		if not onListeningEvent.is_set():
			onListeningEvent.set()

class processInterface(Thread):
	RESPONSE_HANDLED = -1000
	RESPONSE_EXIT = -1001

	def __init__(self, pipeline, reqQ, respQ, mainLoop, onListeningEvent):
		self._pipeline = pipeline
		self._reqQ = reqQ
		self._respQ = respQ
		self._onListeningEvent = onListeningEvent
		self._mainLoop = mainLoop
		self._logger = logging.getLogger(__name__+':processInterface')

		super(processInterface, self).__init__()

		self._actionMap = {
			'isVideoPlaying': self._isVideoPlayingAction,
			'startVideo': self._startVideoAction,
			'stopVideo': self._stopVideoAction,
			'takePhoto': self._takePhotoAction,
			'shutdown': self._shutdownAction
		}

		self.daemon = True
		self._stopped = False

	def run(self):
		self._onListeningEvent.set() # Inform the client that we're ready
		self._onListeningEvent = None # unref
		while not self._stopped:
			self._logger.info('waiting for commands...')
			command = self._reqQ.get()
			self._logger.info('Recieved: %s' % repr(command))

			if self._stopped:
				break

			if command:
				if command['action'] in self._actionMap:
					if 'data' in command:
						kargs = command['data'] or {}
					else:
						kargs = {}

					resp = self._actionMap[command['action']](**kargs)

				else:
					resp = {
						'error': 'command_not_found',
						'details': command
					}

			else:
				resp = {
					'error': 'invalid_command',
					'details': line
				}

			if resp is self.RESPONSE_EXIT:
				break

			elif resp is not self.RESPONSE_HANDLED: #in this case the responsed was handled
				self._sendResponse(resp)

		self._pipeline = None
		self._mainLoop.quit()

	def _isVideoPlayingAction(self):
		return self._pipeline.isVideoStreaming()

	def _startVideoAction(self):
		def doneCb(success):
			if not doneEvent.isSet():
				self._sendResponse(success)
				doneEvent.set()

		doneEvent = Event()
		self._pipeline.playVideo(doneCb)

		if not doneEvent.wait(10.0):
			return {'error': 'timeout'}
		else:
			return self.RESPONSE_HANDLED

	def _stopVideoAction(self):
		def doneCb(success):
			if not doneEvent.isSet():
				self._sendResponse(success)
				doneEvent.set()

		doneEvent = Event()
		self._pipeline.stopVideo(doneCb)

		if not doneEvent.wait(10.0):
			return {'error': 'timeout'}
		else:
			return self.RESPONSE_EXIT

	def _takePhotoAction(self, text=None):
		def doneCb(photo):
			if not doneEvent.isSet():
				if not photo:
					self._sendResponse(None)
				else:
					self._sendResponse(photo, raw= True, b64encode= True)

				doneEvent.set()

		doneEvent = Event()
		self._pipeline.takePhoto(doneCb, text)

		if not doneEvent.wait(7.0):
			return {'error': 'timeout'}
		else:
			return self.RESPONSE_HANDLED

	def _shutdownAction(self):
		self._pipeline.tearDown()
		return self.RESPONSE_EXIT

	def _sendResponse(self, resp, raw= False, b64encode= False):
		if raw:
			if b64encode:
				from base64 import b64encode
				resp = b64encode(resp)

			self._respQ.put(resp)
		else:
			self._respQ.put(resp)

			self._logger.info('Sent: %s' % repr(resp))

	def stop(self):
		self._stopped = True
