# coding=utf-8
__author__ = "AstroPrint Product Team <product@astroprint.com>"
__license__ = "GNU Affero General Public License http://www.gnu.org/licenses/agpl.html"
__copyright__ = "Copyright (C) 2017 3DaGoGo, Inc - Released under terms of the AGPLv3 License"

import logging
import threading

from sys import platform

from collections import deque

from .transport import TransportEvents

class CommsListener(object):
	# ~~~~~~~~~~
	# Implment these functions in the children class to respond to the events
	# ~~~~~~~~~~

	#
	# Called when the link has been opened
	#
	def onLinkOpened(self, link):
		pass

	#
	# Called when the link has an error
	#
	def onLinkError(self, error, description= None):
		pass

	#
	# Called when the link has been closed
	#
	def onLinkClosed(self):
		pass

	#
	# Called when a new command is received from the printer
	#
	def onCommandReceived(self, command):
		pass

	#
	# Called when a new command is sent to the printer
	#
	def onCommandSent(self, command):
		pass

	#
	# Called when a new command is read from the file while an active print job is ongoing. Should process the command and return it
	#
	# - command: The command read from the file
	#
	# - RETURN: an list containing the resulting command sequence after the processing
	#
	def onProcessJobCommand(self, command):
		return [command]

	#
	# Called when the end of the current file has been reached
	#
	def onEndOfFle(self):
		pass

	#
	# Called when there's an error in an ogoing print job
	#
	def onJobError(self, error):
		pass

	#
	# Called when a status command needs to be sent. The implementing class should queue the appropiate commands
	#
	def onStatusCommandsNeeded(self):
		pass


class CommandsComms(TransportEvents):
	def __init__(self, transport, listener):
		self._listener = listener
		self._logger = logging.getLogger(self.__class__.__name__)
		self._serialLogger = logging.getLogger("SERIAL")
		self._serialLoggerEnabled = self._serialLogger.isEnabledFor(logging.DEBUG)
		self._statusPoller = None
		self._printJob = None

		if transport == 'serial':
			from .transport.serial_t import SerialCommTransport
			self._transport = SerialCommTransport(self)

		elif transport == 'usb':
			from .transport.usb_t import UsbCommTransport
			self._transport = UsbCommTransport(self)

		else:
			raise "Invalid transport %s" % transport

		self._sender = CommandSender(self, listener)
		self._sender.start()

	def __del__(self):
		self._logger.debug('Object Removed')
		self.closeLink()

	#
	# Closes the link and performs cleanup
	#
	def closeLink(self):
		self._sender.stop()
		self._printJob = None
		self._transport.closeLink()

	#
	# Return the transport object
	#
	@property
	def transport(self):
		return self._transport

	#
	# Whether the link is open or not
	#
	@property
	def isLinkOpen(self):
		return self._transport.isLinkOpen

	#
	# Return the settings of the active connection. (port, baudrate)
	#
	@property
	def connectionSettings(self):
		return self._transport.connSettings

	#
	# Return whether there's an active print job
	#
	@property
	def printing(self):
		return self._printJob is not None

	#
	# Add the commands to the send queue
	#
	def queueCommands(self, commands):
		for c in commands:
			self.queueCommand(c)

	#
	# Add the commmand to the send queue
	#
	def queueCommand(self, command):
		self._sender.addCommand(command)

	#
	# Add a command to the queue if it's not already there
	#
	def queueCommandIfNotExists(self, command):
		self._sender.addCommandIfNotExists(command)

	#
	# Report that the serial logging has changed
	#
	def serialLoggingChanged(self):
		self._serialLoggerEnabled = self._serialLogger.isEnabledFor(logging.DEBUG)

	#
	# write 'data' on the underlying link
	#
	def writeOnLink(self, data):
		if data is not None:
			self._transport.write(data)
			self._serialLoggerEnabled and self._serialLogger.debug('S: %r' % data)
			self._listener.onCommandSent(data)

	#
	# Starts status poller
	#
	def startStatusPoller(self, interval=5.0):
		if not self._statusPoller:
			self._statusPoller = StatusPoller(self._listener, interval)
			self._statusPoller.start()

	#
	# Stops the status poller
	#
	def stopStatusPoller(self, interval=5.0):
		if self._statusPoller:
			self._statusPoller.stop()
			self._statusPoller = None

	#
	# Set Paused of the status poller
	#
	def setStatusPollerPaused(self, paused):
		if self._statusPoller:
			self._statusPoller.paused = paused

	#
	# Starts printing 'filename'
	#
	def startPrint(self, filename):
		if not self._printJob:
			self._printJob = JobWorker(filename, self, self._listener)
			self._printJob.start()
			#self._printJob.read(1)

	#
	# Stops current print
	#
	def stopPrint(self):
		if self._printJob:
			self._printJob.stop()
			self._printJob = None

	# ~~~~~~~~~~~~~~~~~
	# TransportEvents ~
	# ~~~~~~~~~~~~~~~~~

	def onDataReceived(self, data):
		command = data.strip()
		self._serialLoggerEnabled and self._serialLogger.debug('R: %r' % command)

		try:
			self._listener.onCommandReceived(command)
		except:
			self._logger.error('Error handling command.', exc_info= True)

		if 'ok' in command:
			self._sender.sendNext()
			#when there's an active print job some other things are needed
			if self.printing:
				#read the next command
				self._printJob.read(1)

	def onLinkError(self, error, description= None):
		self._transport.closeLink()
		self._listener.onLinkError(error, description)

	def onLinkInfo(self, info):
		self._serialLoggerEnabled and self._serialLogger.debug(info)


#~~~~~~ Worker to read commands from file

class JobWorker(threading.Thread):
	def __init__(self, filename, comm, eventListener): #eventListener is object of interface CommsListener
		super(JobWorker, self).__init__()
		self._logger = logging.getLogger(self.__class__.__name__)
		self._filename = filename
		self._comm = comm
		self._eventListener = eventListener
		self._stopped = False
		self._fileHandler = None
		self._readEvent = threading.Event()

	def run(self):
		while not self._stopped:
			self._readEvent.wait()
			addedCommands = 0
			if not self._stopped:
				line = self._fileHandler.readline()
				if line == '':
					# end of file reached
					self._eventListener.onEndOfFle()
					self.stop()
				else:
					# strip comments and other wrapping things
					line = line[:line.find(';')].strip()
					if line:
						try:
							processedCmd = self._eventListener.onProcessJobCommand(line)
						except:
							processedCmd = None
							self._logger.error('Error processing job command', exc_info= True)
							self._eventListener.onJobError("error_processing_command")

						if processedCmd is not None:
							self._comm.queueCommands( processedCmd )
							addedCommands += len(processedCmd)

							if addedCommands == self._maxCommands:
								self._readEvent.clear()

	def stop(self):
		self._stopped = True
		self._fileHandler.close()
		self._fileHandler = None
		self._readEvent.set()

	def start(self):
		#open the file
		self._fileHandler = open(self._filename, 'r')
		self._readEvent.clear()
		super(JobWorker, self).start()

	def read(self, maxCommands):
		self._maxCommands = maxCommands
		self._readEvent.set()


#~~~~~~ Worker to request status from the printer

class StatusPoller(threading.Thread):
	def __init__(self, eventListener, interval=5.0):
		super(StatusPoller, self).__init__()
		self._stopped = False
		self._interval = interval
		self._eventListener = eventListener
		self._event = threading.Event()
		self._pauseEvent = threading.Event()
		self._pauseEvent.set()

	def run(self):
		while not self._stopped:
			self._eventListener.onStatusCommandsNeeded()
			self._event.wait(self._interval)
			self._pauseEvent.wait()

	def stop(self):
		self._stopped = True
		self._pauseEvent.set()
		self._event.set()

	@property
	def paused(self):
		return not self._pauseEvent.is_set()

	@paused.setter
	def paused(self, paused):
		if paused:
			self._pauseEvent.clear()
		else:
			self._pauseEvent.set()

#~~~~~~ Worker to empty the command queue

class CommandSender(threading.Thread):
	def __init__(self, comms, eventListener):
		super(CommandSender, self).__init__()
		self._stopped = False
		self._eventListener = eventListener
		self._comms = comms
		self._commandQ = deque()
		self._sendEvent = threading.Event()
		self._readyToSend = False
		self._addingToQueue = threading.Condition()

	def run(self):
		while not self._stopped:

			self._sendEvent.wait()
			if not self._stopped:
				command = None

				try:
					command = str(self._commandQ.pop())
					self._comms.writeOnLink(command)

				except Exception as e:
					if command:
						self._commandQ.append(command) # put back in the queue

					self._eventListener.onLinkError('unable_to_send', "Error: %s, command: %s" % (e, command))

				self._sendEvent.clear()

	def stop(self):
		self._sendEvent.set()
		self._stopped = True

	def sendNext(self):
		if len(self._commandQ):
			self._sendEvent.set()
		else:
			self._readyToSend = True

	def addCommand(self, command):
		if command is not None:
			with self._addingToQueue:
				self._commandQ.appendleft(command)

				if self._readyToSend or len(self._commandQ) == 1:
					self._sendEvent.set()
					self._readyToSend = False

	def addCommandIfNotExists(self, command):
		if command not in self._commandQ:
			self.addCommand(command)