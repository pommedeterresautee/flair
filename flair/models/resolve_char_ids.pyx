#!python
#cython: boundscheck=False, wraparound=False, nonecheck=False, language_level=3

def get_char_index(dic, chunk):
    return get_char_index_cython(dic, chunk)

cdef list get_char_index_cython(dict dic, list chunk):
    cdef list results = list()
    cdef list char_indices
    cdef str text
    cdef str c
    cdef int l = len(chunk)
    cdef int index
    for index in range(l):
        char_indices = [dic.get(c.encode("utf-8"), 0) for c in chunk[index]]
        results.append(char_indices)
    return results


