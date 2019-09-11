#!python
#cython: boundscheck=False, wraparound=False, nonecheck=False, language_level=3

def check(dic, chunk):
    return check_cython(dic, chunk)

cdef list check_cython(dict dic, list chunk):
    cdef list results = list()
    cdef list char_indices = list()
    cdef int encoded_c
    cdef str text
    cdef str c
    for text in chunk:
        for c in text:
            encoded_c = dic.get(c.encode("utf-8"), 0)
            char_indices.append(encoded_c)
        results.append(char_indices)
    return results
