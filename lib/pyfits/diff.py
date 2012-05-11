"""
Facilities for diffing two FITS files.  Includes objects for diffing entire
FITS files, individual HDUs, FITS headers, or just FITS data.

Used to implement the fitsdiff program.
"""


import difflib
import fnmatch
import glob
import os
import textwrap

from collections import defaultdict
from itertools import islice, izip

import numpy as np
from numpy import char

import pyfits
from pyfits.header import Header
from pyfits.hdu.hdulist import fitsopen
from pyfits.hdu.table import _TableLikeHDU
from pyfits.util import StringIO


class _BaseDiff(object):
    """
    Base class for all FITS diff objects.

    When instantiating a FITS diff object, the first two arguments are always
    the two objects to diff (two FITS files, two FITS headers, etc.).
    Instantiating a `_BaseDiff` also causes the diff itself to be executed.
    The returned `_BaseDiff` instance has a number of attribute that describe
    the results of the diff operation.

    The most basic attribute, present on all `_BaseDiff` instances, is
    `.identical` which is `True` if the two objects being compared are
    identical according to the diff method for objects of that type.
    """

    def __init__(self, a, b):
        """
        The `_BaseDiff` class does not implement a `_diff` method and should
        not be instantiated directly. Instead instantiate the appropriate
        subclass of `_BaseDiff` for the objects being compared (for example,
        use `HeaderDiff` to compare two `Header` objects.
        """

        self.a = a
        self.b = b
        self._diff()

    def __nonzero__(self):
        """
        A `_BaseDiff` object acts as `True` in a boolean context if the two
        objects compared are identical.  Otherwise it acts as `False`.
        """

        return not self.identical

    @property
    def identical(self):
        """
        `True` if all the `.diff_*` attributes on this diff instance are empty,
        implying that no differences were found.

        Any subclass of `_BaseDiff` must have at least one `.diff_*` attribute,
        which contains a non-empty value if and only if some difference was
        found between the two objects being compared.
        """

        return not any(getattr(self, attr) for attr in self.__dict__
                       if attr.startswith('diff_'))

    def report(self, fileobj=None):
        """
        Generates a text report on the differences (if any) between two
        objects, and either returns it as a string or writes it to a file-like
        object.

        Parameters
        ----------
        fileobj : file-like object or None (optional)
            If `None`, this method returns the report as a string. Otherwise it
            returns `None` and writes the report to the given file-like object
            (which must have a `.write()` method at a minimum).

        Returns
        -------
        report : str or None
        """

        return_string = False
        if fileobj is None:
            fileobj = StringIO()
            return_string = True

        self._report(fileobj)

        if return_string:
            return fileobj.getvalue()

    def _diff(self):
        raise NotImplementedError

    def _report(self, fileobj):
        raise NotImplementedError


class FITSDiff(_BaseDiff):
    """Diff two FITS files by filename, or two `HDUList` objects.

    `FITSDiff` objects have the following diff attributes:

    - `diff_hdu_count`: If the FITS files being compared have different numbers
      of HDUs, this contains a 2-tuple of the number of HDUs in each file.

    - `diff_hdus`: If any HDUs with the same index are different, this contains
      a list of 2-tuples of the HDU index and the `HDUDiff` object representing
      the differences between the two HDUs.
    """

    def __init__(self, a, b, ignore_keywords=[], ignore_comments=[],
                 ignore_fields=[], numdiffs=10, tolerance=0.0,
                 ignore_blanks=True):
        """
        Parameters
        ----------
        a : str or `HDUList`
            The filename of a FITS file on disk, or an `HDUList` object.

        b : str or `HDUList`
            The filename of a FITS file on disk, or an `HDUList` object to
            compare to the first file.

        ignore_keywords : sequence (optional)
            Header keywords to ignore when comparing two headers; the presence
            of these keywords and their values are ignored.  Wildcard strings
            may also be included in the list.

        ignore_comments : sequence (optional)
            A list of header keywords whose comments should be ignored in the
            comparison.  May contain wildcard strings as with ignore_keywords.

        ignore_fields : sequence (optional)
            The (case-insensitive) names of any table columns to ignore if any
            table data is to be compared.

        numdiffs : int (optional)
            The number of pixel/table values to output when reporting HDU data
            differences.  Though the count of differences is the same either
            way, this allows controlling the number of different values that
            are kept in memory or output.  If a negative value is given, then
            numdifs is treated as unlimited (default: 10).

        tolerance : float (optional)
            The relative difference to allow when comparing two float values
            either in header values, image arrays, or table columns
            (default: 0.0).

        ignore_blanks : bool (optional)
            Ignore extra whitespace at the end of string values either in
            headers or data. Extra leading whitespace is not ignored
            (default: True).
        """

        if isinstance(a, basestring):
            a = fitsopen(a)
            close_a = True
        else:
            close_a = False

        if isinstance(b, basestring):
            b = fitsopen(b)
            close_b = True
        else:
            close_b = False

        self.ignore_keywords = set(ignore_keywords)
        self.ignore_comments = set(ignore_comments)
        self.ignore_fields = set(ignore_fields)
        self.numdiffs = numdiffs
        self.tolerance = tolerance
        self.ignore_blanks = ignore_blanks

        self.diff_hdu_count = ()
        self.diff_hdus = []

        try:
            super(FITSDiff, self).__init__(a, b)
        finally:
            if close_a:
                a.close()
            if close_b:
                b.close()

    def _diff(self):
        if len(self.a) != len(self.b):
            self.diff_hdu_count = (len(self.a), len(self.b))

        # For now, just compare the extensions one by one in order...might
        # allow some more sophisticated types of diffing later...
        # TODO: Somehow or another simplify the passing around of diff
        # options--this will become important as the number of options grows
        for idx in range(min(len(self.a), len(self.b))):
            hdu_diff = HDUDiff(self.a[idx], self.b[idx],
                               ignore_keywords=self.ignore_keywords,
                               ignore_comments=self.ignore_comments,
                               ignore_fields=self.ignore_fields,
                               numdiffs=self.numdiffs,
                               tolerance=self.tolerance,
                               ignore_blanks=self.ignore_blanks)

            if not hdu_diff.identical:
                self.diff_hdus.append((idx, hdu_diff))

    def _report(self, fileobj):
        wrapper = textwrap.TextWrapper(initial_indent='  ',
                                       subsequent_indent='  ')

        # print out heading and parameter values
        filenamea = self.a.filename()
        if not filenamea:
            filenamea = '<%s object at 0x%x>' % (self.a.__class__.__name__,
                                                 id(self.a))

        filenameb = self.b.filename()
        if not filenameb:
            filenameb = '<%s object at 0x%x>' % (self.b.__class__.__name__,
                                                 id(self.b))

        fileobj.write('\n fitsdiff: %s\n' % pyfits.__version__)
        fileobj.write(' a: %s\n b: %s\n' % (filenamea, filenameb))
        if self.ignore_keywords:
            ignore_keywords = ' '.join(sorted(self.ignore_keywords))
            fileobj.write(' Keyword(s) not to be compared:\n%s\n' %
                          wrapper.fill(ignore_keywords))

        if self.ignore_comments:
            ignore_comments = ' '.join(sorted(self.ignore_comments))
            fileobj.write(' Keyword(s) whose comments are not to be compared:'
                          '\n%s\n' % wrapper.fill(ignore_keywords))
        if self.ignore_fields:
            ignore_fields = ' '.join(sorted(self.ignore_fields))
            fileobj.write(' Table column(s) not to be compared:\n%s\n' %
                          wrapper.fill(ignore_fields))
        fileobj.write(' Maximum number of different data values to be '
                      'reported: %s\n' % self.numdiffs)
        fileobj.write(' Data comparison level: %s\n' % self.tolerance)

        if self.diff_hdu_count:
            fileobj.write('\nFiles contain different numbers of HDUs:\n')
            fileobj.write(' a: %d\n' % self.diff_hdu_count[0])
            fileobj.write(' b: %d\n' % self.diff_hdu_count[1])

            if not self.diff_hdus:
                fileobj.write('No differences found between common HDUs.\n')
                return
        elif not self.diff_hdus:
            fileobj.write('\nNo differences found.\n')
            return

        for idx, hdu_diff in self.diff_hdus:
            # print out the extension heading
            if idx == 0:
                fileobj.write('\nPrimary HDU:\n')
            else:
                fileobj.write('\nExtension HDU %d:\n' % idx)
            hdu_diff._report(fileobj)


class HDUDiff(_BaseDiff):
    """
    Diff two HDU objects, including their headers and their data (but only if
    both HDUs contain the same type of data (image, table, or unknown).

    `HDUDiff` objects have the following diff attributes:

    - `diff_extnames`: If the two HDUs have different EXTNAME values, this
      contains a 2-tuple of the different extension names.

    - `diff_extvers`: If the two HDUS have different EXTVER values, this
      contains a 2-tuple of the different extension versions.

    - `diff_extension_types`: If the two HDUs have different XTENSION values,
      this contains a 2-tuple of the different extension types.

    - `diff_headers`: Contains a `HeaderDiff` object for the headers of the two
      HDUs. This will always contain an object--it may be determined whether
      the headers are different through `diff_headers.identical`.

    - `diff_data`: Contains either a `ImageDataDiff`, `TableDataDiff`, or
      `RawDataDiff` as appropriate for the data in the HDUs, and only if the
      two HDUs have non-empty data of the same type (`RawDataDiff` is used for
      HDUs containing non-empty data of an indeterminate type).
    """

    def __init__(self, a, b, ignore_keywords=[], ignore_comments=[],
                 ignore_fields=[], numdiffs=10, tolerance=0.0,
                 ignore_blanks=True):
        """
        Parameters
        ----------
        See `FITSDiff` for explanations of these parameters.
        """

        self.ignore_keywords = set(ignore_keywords)
        self.ignore_comments = set(ignore_comments)
        self.ignore_fields = set(ignore_fields)
        self.tolerance = tolerance
        self.numdiffs = numdiffs
        self.ignore_blanks = ignore_blanks

        self.diff_extnames = ()
        self.diff_extvers = ()
        self.diff_extension_types = ()
        self.diff_headers = None
        self.diff_data = None

        super(HDUDiff, self).__init__(a, b)

    def _diff(self):
        if self.a.name != self.b.name:
            self.diff_extnames = (self.a.name, self.b.name)

        # TODO: All extension headers should have a .extver attribute;
        # currently they have a hidden ._extver attribute, but there's no
        # reason it should be hidden
        if self.a.header.get('EXTVER') != self.b.header.get('EXTVER'):
            self.diff_extvers = (self.a.header.get('EXTVER'),
                                 self.b.header.get('EXTVER'))

        if self.a.header.get('XTENSION') != self.b.header.get('XTENSION'):
            self.diff_extension_types = (self.a.header.get('XTENSION'),
                                         self.b.header.get('XTENSION'))

        self.diff_headers = HeaderDiff(self.a.header, self.b.header,
                                       ignore_keywords=self.ignore_keywords,
                                       ignore_comments=self.ignore_comments,
                                       tolerance=self.tolerance,
                                       ignore_blanks=self.ignore_blanks)

        if self.a.data is None or self.b.data is None:
            # TODO: Perhaps have some means of marking this case
            pass
        elif self.a.is_image and self.b.is_image:
            self.diff_data = ImageDataDiff(self.a.data, self.b.data,
                                           numdiffs=self.numdiffs,
                                           tolerance=self.tolerance)
        elif (isinstance(self.a, _TableLikeHDU) and
              isinstance(self.b, _TableLikeHDU)):
            # TODO: Replace this if/when _BaseHDU grows a .is_table property
            self.diff_data = TableDataDiff(self.a.data, self.b.data)
        elif not self.diff_extension_types:
            # Don't diff the data for unequal extension types that are not
            # recognized image or table types
            self.diff_data = RawDataDiff(self.a.data, self.b.data)

    def _report(self, fileobj):
        if self.identical:
            fileobj.write(" No differences found.\n")
        if self.diff_extension_types:
            fileobj.write(" Extension types differ:\n  a: %s\n  b: %s\n" %
                          self.diff_extension_types)
        if self.diff_extnames:
            fileobj.write(" Extension names differ:\n  a: %s\n  b: %s\n" %
                          self.diff_extnames)
        if self.diff_extvers:
            fileobj.write(" Extension versions differ:\n  a: %s\n  b: %s\n" %
                          self.diff_extvers)

        if not self.diff_headers.identical:
            fileobj.write("\n Headers contain differences:\n")
            self.diff_headers._report(fileobj)

        if self.diff_data is not None and not self.diff_data.identical:
            fileobj.write("\n Data contains differences:\n")
            self.diff_data._report(fileobj)


class HeaderDiff(_BaseDiff):
    """
    Diff two `Header` objects.

    `HeaderDiff` objects have the following diff attributes:

    - `diff_keyword_count`: If the two headers contain a different number of
      keywords, this contains a 2-tuple of the keyword count for each header.

    - `diff_keywords`: If either header contains one or more keywords that
      don't appear at all in the other header, this contains a 2-tuple
      consisting of a list of the keywords only appearing in header a, and a
      list of the keywords only appearing in header b.

    - `diff_duplicate_keywords`: If a keyword appears in both headers at least
      once, but contains a different number of duplicates (for example, a
      different number of HISTORY cards in each header), an item is added to
      this dict with the keyword as the key, and a 2-tuple of the different
      counts of that keyword as the value.  For example::

          {'HISTORY': (20, 19)}

      means that header a contains 20 HISTORY cards, while header b contains
      only 19 HISTORY cards.

    - `diff_keyword_values`: If any of the common keyword between the two
      headers have different values, they appear in this dict.  It has a
      structure similar to `diff_duplicate_keywords`, with the keyword as the
      key, and a 2-tuple of the different values as the value.  For example::

          {'NAXIS': (2, 3)}

      means that the NAXIS keyword has a value of 2 in header a, and a value of
      3 in header b.  This excludes any keywords matched by the
      `ignore_keywords` list.

    - `diff_keyword_comments`: Like `diff_keyword_values`, but contains
      differences between keyword comments.

    `HeaderDiff` objects also have a `common_keywords` attribute that lists all
    keywords that appear in both headers.
    """

    def __init__(self, a, b, ignore_keywords=[], ignore_comments=[],
                 tolerance=0.0, ignore_blanks=True):
        """
        Parameters
        ----------
        See `FITSDiff` for explanations of these parameters.
        """

        self.ignore_keywords = set(ignore_keywords)
        self.ignore_comments = set(ignore_comments)
        self.tolerance = tolerance
        self.ignore_blanks = ignore_blanks

        self.ignore_keyword_patterns = set()
        self.ignore_comment_patterns = set()
        for keyword in list(self.ignore_keywords):
            if keyword != '*' and glob.has_magic(keyword):
                self.ignore_keywords.remove(keyword)
                self.ignore_keyword_patterns.add(keyword)
        for keyword in list(self.ignore_comments):
            if keyword != '*' and glob.has_magic(keyword):
                self.ignore_comments.remove(keyword)
                self.ignore_comment_patterns.add(keyword)

        # Keywords appearing in each header
        self.common_keywords = []

        # Set to the number of keywords in each header if the counts differ
        self.diff_keyword_count = ()

        # Set if the keywords common to each header (excluding ignore_keywords)
        # appear in different positions within the header
        # TODO: Implement this
        self.diff_keyword_positions = ()

        # Keywords unique to each header (excluding keywords in
        # ignore_keywords)
        self.diff_keywords = ()

        # Keywords that have different numbers of duplicates in each header
        # (excluding keywords in ignore_keywords)
        self.diff_duplicate_keywords = {}

        # Keywords common to each header but having different values (excluding
        # keywords in ignore_keywords)
        self.diff_keyword_values = defaultdict(lambda: [])

        # Keywords common to each header but having different comments
        # (excluding keywords in ignore_keywords or in ignore_comments)
        self.diff_keyword_comments = defaultdict(lambda: [])

        if isinstance(a, basestring):
            a = Header.fromstring(a)
        if isinstance(b, basestring):
            b = Header.fromstring(b)

        if not (isinstance(a, Header) and isinstance(b, Header)):
            raise TypeError('HeaderDiff can only diff pyfits.Header objects '
                            'or strings containing FITS headers.')

        super(HeaderDiff, self).__init__(a, b)

    # TODO: This doesn't pay much attention to the *order* of the keywords,
    # except in the case of duplicate keywords.  The order should be checked
    # too, or at least it should be an option.
    def _diff(self):
        # build dictionaries of keyword values and comments
        def get_header_values_comments(header):
            values = {}
            comments = {}
            for card in header.cards:
                value = card.value
                if self.ignore_blanks and isinstance(value, basestring):
                    value = value.rstrip()
                values.setdefault(card.keyword, []).append(value)
                comments.setdefault(card.keyword, []).append(card.comment)
            return values, comments

        valuesa, commentsa = get_header_values_comments(self.a)
        valuesb, commentsb = get_header_values_comments(self.b)

        keywordsa = set(valuesa)
        keywordsb = set(valuesb)

        self.common_keywords = sorted(keywordsa.intersection(keywordsb))
        if len(self.a) != len(self.b):
            self.diff_keyword_count = (len(self.a), len(self.b))

        # Any other diff attributes should exclude ignored keywords
        keywordsa = keywordsa.difference(self.ignore_keywords)
        keywordsb = keywordsb.difference(self.ignore_keywords)
        if self.ignore_keyword_patterns:
            for pattern in self.ignore_keyword_patterns:
                keywordsa = keywordsa.difference(fnmatch.filter(keywordsa,
                                                                pattern))
                keywordsb = keywordsb.difference(fnmatch.filter(keywordsb,
                                                                pattern))

        if '*' in self.ignore_keywords:
            # Any other differences between keywords are to be ignored
            return

        left_only_keywords = sorted(keywordsa.difference(keywordsb))
        right_only_keywords = sorted(keywordsb.difference(keywordsa))

        if left_only_keywords or right_only_keywords:
            self.diff_keywords = (left_only_keywords, right_only_keywords)

        # Compare count of each common keyword
        for keyword in self.common_keywords:
            if keyword in self.ignore_keywords:
                continue
            if self.ignore_keyword_patterns:
                skip = False
                for pattern in self.ignore_keyword_patterns:
                    if fnmatch.fnmatch(keyword, pattern):
                        skip = True
                        break
                if skip:
                    continue

            counta = len(valuesa[keyword])
            countb = len(valuesb[keyword])
            if counta != countb:
                self.diff_duplicate_keywords[keyword] = (counta, countb)

            # Compare keywords' values and comments
            for a, b in zip(valuesa[keyword], valuesb[keyword]):
                if diff_values(a, b, tolerance=self.tolerance):
                    self.diff_keyword_values[keyword].append((a, b))
                else:
                    # If there are duplicate keywords we need to be able to
                    # index each duplicate; if the values of a duplicate
                    # are identical use None here
                    self.diff_keyword_values[keyword].append(None)

            if not any(self.diff_keyword_values[keyword]):
                # No differences found; delete the array of Nones
                del self.diff_keyword_values[keyword]

            if '*' in self.ignore_comments or keyword in self.ignore_comments:
                continue
            if self.ignore_comment_patterns:
                skip = False
                for pattern in self.ignore_comment_patterns:
                    if fnmatch.fnmatch(keyword, pattern):
                        skip = True
                        break
                if skip:
                    continue

            for a, b in zip(commentsa[keyword], commentsb[keyword]):
                if diff_values(a, b):
                    self.diff_keyword_comments[keyword].append((a, b))
                else:
                    self.diff_keyword_comments[keyword].append(None)

            if not any(self.diff_keyword_comments[keyword]):
                del self.diff_keyword_comments[keyword]

    def _report(self, fileobj):
        if self.diff_keyword_count:
            fileobj.write('  Headers have different number of cards:\n')
            fileobj.write('   a: %d\n' % self.diff_keyword_count[0])
            fileobj.write('   b: %d\n' % self.diff_keyword_count[1])
        if self.diff_keywords:
            for keyword in self.diff_keywords[0]:
                fileobj.write('  Extra keyword %-8s in a\n' % keyword)
            for keyword in self.diff_keywords[1]:
                fileobj.write('  Extra keyword %-8s in b\n' % keyword)

        if self.diff_duplicate_keywords:
            for keyword, count in sorted(self.diff_duplicate_keywords.items()):
                fileobj.write('  Inconsistent duplicates of keyword %-8s:\n' %
                              keyword)
                fileobj.write('   Occurs %d times in a, %d times in b\n' %
                              count)

        if self.diff_keyword_values or self.diff_keyword_comments:
            for keyword in self.common_keywords:
                report_diff_keyword_attr(fileobj, 'values',
                                         self.diff_keyword_values, keyword)
                report_diff_keyword_attr(fileobj, 'comments',
                                         self.diff_keyword_comments, keyword)

# TODO: It might be good if there was also a threshold option for percentage of
# different pixels: For example ignore if only 1% of the pixels are different
# within some threshold.  There are lots of possibilities here, but hold off
# for now until specific cases come up.
class ImageDataDiff(_BaseDiff):
    """
    Diff two image data arrays (really any array from a PRIMARY HDU or an IMAGE
    extension HDU, though the data unit is assumed to be "pixels").

    `ImageDataDiff` objects have the following diff attributes:

    - `diff_dimensions`: If the two arrays contain either a different number of
      dimensions or different sizes in any dimension, this contains a 2-tuple
      of the shapes of each array.  Currently no further comparison is
      performed on images that don't have the exact same dimensions.

    - `diff_pixels`: If the two images contain any different pixels, this
      contains a list of 2-tuples of the array index where the difference was
      found, and another 2-tuple containing the different values.  For example,
      if the pixel at (0, 0) contains different values this would look like::

          [(0, 0), (1.1, 2.2)]

      where 1.1 and 2.2 are the values of that pixel in each array.  This
      array only contains up to `self.numdiffs` differences, for storage
      efficiency.

    - `diff_total`: The total number of different pixels found between the
      arrays.  Although `diff_pixels` does not necessarily contain all the
      different pixel values, this can be used to get a count of the total
      number of differences found.

    - `diff_ratio`: Contains the ratio of `diff_total` to the total number of
      pixels in the arrays.
    """

    def __init__(self, a, b, numdiffs=10, tolerance=0.0):
        """
        Parameters
        ----------
        See `FITSDiff` for explanations of these parameters.
        """

        self.numdiffs = numdiffs
        self.tolerance = tolerance

        self.diff_dimensions = ()
        self.diff_pixels = []
        self.diff_ratio = 0

        # self.diff_pixels only holds up to numdiffs differing pixels, but this
        # self.diff_total stores the total count of differences between
        # the images, but not the different values
        self.diff_total = 0

        super(ImageDataDiff, self).__init__(a, b)

    def _diff(self):
        if self.a.shape != self.b.shape:
            self.diff_dimensions = (self.a.shape, self.b.shape)
            # Don't do any further comparison if the dimensions differ
            # TODO: Perhaps we could, however, diff just the intersection
            # between the two images
            return

        # Find the indices where the values are not equal
        # If neither a nor b are floating point, ignore self.tolerance
        if not ((np.issubdtype(self.a.dtype, float) or
                 np.issubdtype(self.a.dtype, complex)) or
                (np.issubdtype(self.b.dtype, float) or
                 np.issubdtype(self.b.dtype, complex))):
            tolerance = 0
        else:
            tolerance = self.tolerance

        diffs = where_not_allclose(self.a, self.b, atol=0.0, rtol=tolerance)

        self.diff_total = len(diffs[0])

        if self.diff_total == 0:
            # Then we're done
            return

        if self.numdiffs < 0:
            numdiffs = self.diff_total
        else:
            numdiffs = self.numdiffs

        self.diff_pixels = [(idx, (self.a[idx], self.b[idx]))
                            for idx in islice(izip(*diffs), 0, numdiffs)]
        self.diff_ratio = float(self.diff_total) / float(len(self.a.flat))

    def _report(self, fileobj):
        if self.diff_dimensions:
            fileobj.write('  Data dimensions differ:\n')
            fileobj.write('   a: %s\n' %
                          ' x '.join(reversed(self.diff_dimensions[0])))
            fileobj.write('   b: %s\n' %
                          ' x '.join(reversed(self.diff_dimensions[1])))
            # For now we don't do any further comparison if the dimensions
            # differ; though in the future it might be nice to be able to
            # compare at least where the images intersect
            fileobj.write('  No further data comparison performed.\n')
            return

        if not self.diff_pixels:
            return

        for index, values in self.diff_pixels:
            index = [x + 1 for x in reversed(index)]
            fileobj.write('  Data differs at %s:\n' % index)
            report_diff_values(fileobj, values[0], values[1])

        fileobj.write('  ...\n')
        fileobj.write('  %d different pixels found (%.2f%% different).\n' %
                      (self.diff_total, self.diff_ratio * 100))


class RawDataDiff(ImageDataDiff):
    """
    RawDataDiff is just a special case of ImageDataDiff where the images are
    one-dimensional, and the data is treated as bytes instead of pixel values.
    """

    def __init__(self, a, b, numdiffs=10):
        self.diff_dimensions = ()
        self.diff_bytes = []

        super(RawDataDiff, self).__init__(a, b, numdiffs=numdiffs)

    def _diff(self):
        super(RawDataDiff, self)._diff()
        if self.diff_dimensions:
            self.diff_dimensions = (self.diff_dimensions[0][0],
                                    self.diff_dimensions[1][0])

        self.diff_bytes = [(x[0], y) for x, y in self.diff_pixels]
        del self.diff_pixels

    def _report(self, fileobj):
        if self.diff_dimensions:
            fileobj.write('  Data sizes differ:\n')
            fileobj.write('   a: %d bytes\n' % self.diff_dimensions[0])
            fileobj.write('   b: %d bytes\n' % self.diff_dimensions[1])
            # For now we don't do any further comparison if the dimensions
            # differ; though in the future it might be nice to be able to
            # compare at least where the images intersect
            fileobj.write('  No further data comparison performed.\n')
            return

        if not self.diff_bytes:
            return

        for index, values in self.diff_bytes:
            fileobj.write('  Data differs at byte %d:\n' % index)
            report_diff_values(fileobj, values[0], values[1])

        fileobj.write('  ...\n')
        fileobj.write('  %d different bytes found (%.2f%% different).\n' %
                      (self.diff_total, self.diff_ratio * 100))

class TableDataDiff(_BaseDiff):
    def __init__(self, a, b, ignore_fields=[], numdiffs=10, tolerance=0.0):
        self.ignore_fields = set(ignore_fields)
        self.numdiffs = numdiffs
        self.tolerance = tolerance

        self.common_columns = []
        self.common_column_names = set()

        # self.diff_columns contains columns with different column definitions,
        # but not different column data. Column data is only compared in
        # columns that have the same definitions
        self.diff_column_count = ()
        self.diff_columns = ()

        # Like self.diff_columns, but just contains a list of the column names
        # unique to each table, and in the order they appear in the tables
        self.diff_column_names = ()
        self.diff_values = []

        self.diff_ratio = 0
        self.diff_total = 0

        super(TableDataDiff, self).__init__(a, b)

    def _diff(self):
        # Much of the code for comparing columns is similar to the code for
        # comparing headers--consider refactoring
        colsa = self.a.columns
        colsb = self.b.columns

        if len(colsa) != len(colsb):
            self.diff_column_count = (len(colsa), len(colsb))

        # Even if the number of columns are unequal, we still do comparison of
        # any common columns
        colsa = set(colsa)
        colsb = set(colsb)

        if '*' in self.ignore_fields:
            # If all columns are to be ignored, ignore any further differences
            # between the columns
            return

        # Keep the user's original ignore_fields list for reporting purposes,
        # but internally use a case-insensitive version
        ignore_fields = set([f.lower() for f in self.ignore_fields])

        # It might be nice if there were a cleaner way to do this, but for now
        # it'll do
        for fieldname in ignore_fields:
            for col in list(colsa):
                if col.name.lower() == fieldname:
                    colsa.remove(col)
            for col in list(colsb):
                if col.name.lower() == fieldname:
                    colsb.remove(col)

        self.common_columns = sorted(colsa.intersection(colsb))

        self.common_column_names = set([col.name.lower()
                                        for col in self.common_columns])

        left_only_columns = dict((col.name.lower(), col)
                                 for col in colsa.difference(colsb))
        right_only_columns = dict((col.name.lower(), col)
                                  for col in colsb.difference(colsa))

        if left_only_columns or right_only_columns:
            self.diff_columns = (left_only_columns, right_only_columns)
            self.diff_column_names = ([], [])

        if left_only_columns:
            for col in self.a.columns:
                if col.name.lower() in left_only_columns:
                    self.diff_column_names[0].append(col.name)

        if right_only_columns:
            for col in self.b.columns:
                if col.name.lower() in right_only_columns:
                    self.diff_column_names[1].append(col.name)

        # Like in the old fitsdiff, compare tables on a column by column basis
        # The difficulty here is that, while FITS column names are meant to be
        # case-insensitive, PyFITS still allows, for the sake of flexibility,
        # two columns with the same name but different case.  When columns are
        # accessed in FITS tables, a case-sensitive is tried first, and failing
        # that a case-insensitive match is made.
        # It's conceivable that the same column could appear in both tables
        # being compared, but with different case.
        # Though it *may* lead to inconsistencies in these rare cases, this
        # just assumes that there are no duplicated column names in either
        # table, and that the column names can be treated case-insensitively.
        for col in self.common_columns:
            if col.name.lower() in ignore_fields:
                continue
            cola = self.a[col.name]
            colb = self.b[col.name]
            if (np.issubdtype(cola.dtype, float) and
                np.issubdtype(colb.dtype, float)):
                diffs = where_not_allclose(cola, colb, atol=0.0,
                                           rtol=self.tolerance)
            elif 'P' in col.format:
                diffs = ([idx for idx in xrange(len(cola))
                          if not np.allclose(cola[idx], colb[idx], atol=0.0,
                                             rtol=self.tolerance)],)
            else:
                diffs = np.where(cola != colb)

            self.diff_total += len(set(diffs[0]))

            if self.numdiffs >= 0:
                if len(self.diff_values) >= self.numdiffs:
                    # Don't save any more diff values
                    continue

                # Add no more diff'd values than this
                max_diffs = self.numdiffs - len(self.diff_values)
            else:
                max_diffs = len(diffs[0])

            last_seen_idx = None
            for idx in islice(diffs[0], 0, max_diffs):
                if idx == last_seen_idx:
                    # Skip duplicate indices, which my occur when the column
                    # data contains multi-dimensional values; we're only
                    # interested in storing row-by-row differences
                    continue
                last_seen_idx = idx
                self.diff_values.append(((col.name, idx),
                                         (cola[idx], colb[idx])))

        total_values = len(self.a) * len(self.a.dtype.fields)
        self.diff_ratio = float(self.diff_total) / float(total_values)

    def _report(self, fileobj):
        if self.diff_column_count:
            fileobj.write('  Tables have different number of columns:\n')
            fileobj.write('   a: %d\n' % self.diff_column_count[0])
            fileobj.write('   b: %d\n' % self.diff_column_count[1])

        if self.diff_column_names:
            # Show columns with names unique to either table
            for name in self.diff_column_names[0]:
                format = self.diff_columns[0][name.lower()].format
                fileobj.write('  Extra column %s of format %s in a\n' %
                              (name, format))
            for name in self.diff_column_names[1]:
                format = self.diff_columns[1][name.lower()].format
                fileobj.write('  Extra column %s of format %s in b\n' %
                              (name, format))

            # Now go through each table again and show columns with common
            # names but other property differences...
            # Column attributes of interest for comparison
            colattrs = [('format', 'formats'), ('unit', 'units'),
                        ('null', 'null values'), ('bscale', 'bscales'),
                        ('bzero', 'bzeros'), ('disp', 'display formats'),
                        ('dim', 'dimensions')]
            for name in self.diff_column_names[0]:
                name = name.lower()
                if name not in self.common_column_names:
                    # A column with this name appears in both tables, but is
                    # otherwise somehow different...
                    continue
                cola = self.diff_column_names[0][name]
                colb = self.diff_column_names[1][name]
                for attr, descr in colattrs:
                    vala = getattr(cola, attr)
                    valb = getattr(colb, attr)
                    if vala == valb:
                        continue
                    fileobj.write('  Column %s has different %s:\n' %
                                  (cola.name, descr))
                    report_diff_values(fileobj, vala, valb)

        if not self.diff_values:
            return

        # Finally, let's go through and report column data differences:
        for indx, values in self.diff_values:
            fileobj.write('  Column %s data differs in row %d:\n' % indx)
            report_diff_values(fileobj, values[0], values[1])

        if self.diff_values and self.numdiffs < self.diff_total:
            fileobj.write('  ...%d additional difference(s) found.\n' %
                          (self.diff_total - self.numdiffs))

        fileobj.write('  ...\n')
        fileobj.write('  %d different table data values found '
                      '(%.2f%% different).\n' %
                      (self.diff_total, self.diff_ratio * 100))


def diff_values(a, b, tolerance=0.0):
    """
    Diff two scalar values.  If both values are floats they are compared to
    within the given relative tolerance.
    """

    # TODO: Handle ifs and nans
    if isinstance(a, float) and isinstance(b, float):
        return not np.allclose(a, b, tolerance, 0.0)
    else:
        return a != b


def report_diff_values(fileobj, a, b):
    """Write a diff between two values to the specified file-like object."""

    #import pdb; pdb.set_trace()
    for line in difflib.ndiff(str(a).splitlines(), str(b).splitlines()):
        if line[0] == '-':
            line = 'a>' + line[1:]
        elif line[0] == '+':
            line = 'b>' + line[1:]
        else:
            line = ' ' + line
        fileobj.write('   %s\n' % line.rstrip('\n'))


def report_diff_keyword_attr(fileobj, attr, diffs, keyword):
    """
    Write a diff between two header keyword values or comments to the specified
    file-like object.
    """

    if keyword in diffs:
        vals = diffs[keyword]
        for idx, val in enumerate(vals):
            if val is None:
                continue
            if idx == 0:
                ind = ''
            else:
                ind = '[%d]' % (idx + 1)
            fileobj.write('  Keyword %-8s%s has different %s:\n' %
                          (keyword, ind, attr))
            report_diff_values(fileobj, val[0], val[1])


def where_not_allclose(a, b, rtol=1e-5, atol=1e-8):
    """
    A version of numpy.allclose that returns the indices where the two arrays
    differ, instead of just a boolean value.
    """

    # TODO: Handle ifs and nans
    if atol == 0.0 and rtol == 0.0:
        # Use a faster comparison for the most simple (and common) case
        return np.where(a != b)
    return np.where(np.abs(a - b) > (atol + rtol * np.abs(b)))
