import gzip
import os

from pyfits.file import _File
from pyfits.hdu.base import _BaseHDU
from pyfits.hdu.hdulist import HDUList
from pyfits.hdu.image import PrimaryHDU
from pyfits.util import _pad_length

class StreamingHDU(object):
    """
    A class that provides the capability to stream data to a FITS file
    instead of requiring data to all be written at once.

    The following pseudocode illustrates its use::

        header = pyfits.Header()

        for all the cards you need in the header:
            header.update(key,value,comment)

        shdu = pyfits.StreamingHDU('filename.fits',header)

        for each piece of data:
            shdu.write(data)

        shdu.close()
    """

    def __init__(self, name, header):
        """
        Construct a `StreamingHDU` object given a file name and a header.

        Parameters
        ----------
        name : file path, file object, or file like object
            The file to which the header and data will be streamed.
            If opened, the file object must be opened for append
            (ab+).

        header : `Header` instance
            The header object associated with the data to be written
            to the file.

        Notes
        -----
        The file will be opened and the header appended to the end of
        the file.  If the file does not already exist, it will be
        created, and if the header represents a Primary header, it
        will be written to the beginning of the file.  If the file
        does not exist and the provided header is not a Primary
        header, a default Primary HDU will be inserted at the
        beginning of the file and the provided header will be added as
        the first extension.  If the file does already exist, but the
        provided header represents a Primary header, the header will
        be modified to an image extension header and appended to the
        end of the file.
        """

        if isinstance(name, gzip.GzipFile):
            raise TypeError('StreamingHDU not supported for GzipFile objects.')

        self._header = header.copy()

        # handle a file object instead of a file name

        if isinstance(name, file):
           filename = name.name
        elif isinstance(name, basestring):
            filename = name
        else:
            filename = ''
#
#       Check if the file already exists.  If it does not, check to see
#       if we were provided with a Primary Header.  If not we will need
#       to prepend a default PrimaryHDU to the file before writing the
#       given header.
#
        newFile = False

        if filename:
            if not os.path.exists(filename) or os.path.getsize(filename) == 0:
                newFile = True
        elif (hasattr(name, 'len') and name.len == 0):
            newFile = True

        if not 'SIMPLE' in self._header:
            if newFile:
                hdulist = HDUList([PrimaryHDU()])
                hdulist.writeto(name, 'exception')
            else:
#
#               This will not be the first extension in the file so we
#               must change the Primary header provided into an image
#               extension header.
#
                self._header.update('XTENSION','IMAGE','Image extension',
                                    after='SIMPLE')
                del self._header['SIMPLE']

                if 'PCOUNT' not in self._header:
                    dim = self._header['NAXIS']

                    if dim == 0:
                        dim = ''
                    else:
                        dim = str(dim)

                    self._header.update('PCOUNT', 0, 'number of parameters',
                                        after='NAXIS' + dim)

                if 'GCOUNT' not in self._header:
                    self._header.update('GCOUNT', 1, 'number of groups',
                                        after='PCOUNT')

        self._ffo = _File(name, 'append')

        # This class doesn't keep an internal data attribute, so this will
        # always be false
        self._data_loaded = False

        # TODO : Fix this once the HDU writing API is cleaned up
        tmp_hdu = _BaseHDU(header=self._header)
        self._hdrLoc = tmp_hdu._writeheader(self._ffo)[0]
        self._datLoc = self._ffo.tell()
        self._size = self.size()

        if self._size != 0:
            self.writeComplete = 0
        else:
            self.writeComplete = 1

    # Support the 'with' statement
    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()

    def write(self, data):
        """
        Write the given data to the stream.

        Parameters
        ----------
        data : ndarray
            Data to stream to the file.

        Returns
        -------
        writeComplete : int
            Flag that when `True` indicates that all of the required
            data has been written to the stream.

        Notes
        -----
        Only the amount of data specified in the header provided to
        the class constructor may be written to the stream.  If the
        provided data would cause the stream to overflow, an `IOError`
        exception is raised and the data is not written.  Once
        sufficient data has been written to the stream to satisfy the
        amount specified in the header, the stream is padded to fill a
        complete FITS block and no more data will be accepted.  An
        attempt to write more data after the stream has been filled
        will raise an `IOError` exception.  If the dtype of the input
        data does not match what is expected by the header, a
        `TypeError` exception is raised.
        """

        curDataSize = self._ffo.tell() - self._datLoc

        if self.writeComplete or curDataSize + data.nbytes > self._size:
            raise IOError('Attempt to write more data to the stream than the '
                          'header specified.')

        if _ImageBaseHDU.NumCode[self._header['BITPIX']] != data.dtype.name:
            raise TypeError('Supplied data does not match the type specified '
                            'in the header.')

        if data.dtype.str[0] != '>':
#
#           byteswap little endian arrays before writing
#
            output = data.byteswap()
        else:
            output = data

        self._ffo.write(output)

        if self._ffo.tell() - self._datLoc == self._size:
#
#           the stream is full so pad the data to the next FITS block
#
            self._ffo.file.write(_pad_length(self._size) * '\0')
            self.writeComplete = 1

        self._ffo.flush()

        return self.writeComplete

    def size(self):
        """
        Return the size (in bytes) of the data portion of the HDU.
        """

        size = 0
        naxis = self._header.get('NAXIS', 0)

        if naxis > 0:
            simple = self._header.get('SIMPLE', 'F')
            randomGroups = self._header.get('GROUPS', 'F')

            if simple == 'T' and randomGroups == 'T':
                groups = 1
            else:
                groups = 0

            size = 1

            for idx in range(groups,naxis):
                size = size * self._header['NAXIS' + str(idx + 1)]
            bitpix = self._header['BITPIX']
            gcount = self._header.get('GCOUNT', 1)
            pcount = self._header.get('PCOUNT', 0)
            size = abs(bitpix) * gcount * (pcount + size) // 8
        return size

    def close(self):
        """
        Close the physical FITS file.
        """

        self._ffo.close()
