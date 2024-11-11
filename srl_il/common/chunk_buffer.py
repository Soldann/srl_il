import numpy as np
import torch

class ChunkBuffer:
    """
    This is a helper type. It is a circular queue used for storing and retrieving chunks of data.
    """
    def __init__(self, shape,  chunk_dim, chunk_length, max_length=None, dtype=torch.float32, device=None):
        """
        args:
            shape: the shape of the data. Example (B, T, H, W, C) and set the chunk_dim to 1 then the queue grows in the T dimension
            chunk_dim: the dimension of the chunks
            chunk_length: the length of the chunks. Each pop will return a chunk of this length
            dtype: the dtype of the chunks 
            max_length: the maximum length of the buffer. If None, the buffer will be three times the chunk_length
        """
        self._chunk_length = chunk_length
        self._max_length = max_length if max_length is not None else 3*chunk_length
        self._shape = shape
        self._chunk_dim = chunk_dim
        assert chunk_dim < len(shape), "chunk_dim should be less than the length of the shape"
        assert chunk_length>0, "chunk_length should be greater than 0"
        assert self._max_length>self._chunk_length*2, "max_length should be greater than chunk_length"
        buffer_shape = list(shape)
        buffer_shape[chunk_dim] = self._max_length
        self._buffer = torch.zeros(buffer_shape, dtype=dtype, device=device)

        self.index = chunk_length
        self.have_rolledback = False

    def _rollback(self):
        """
        rollback the clice(self.index-self.chunk_length, self.index) to be the slice(0, self.chunk_length)
        """
        slice_index_src = [slice(None)]*len(self._buffer.shape)
        slice_index_src[self._chunk_dim] = slice(self.index-self._chunk_length, self.index)
        slice_index_dst = [slice(None)]*len(self._buffer.shape)
        slice_index_dst[self._chunk_dim] = slice(0, self._chunk_length)
        self._buffer[tuple(slice_index_dst)] = self._buffer[tuple(slice_index_src)]
        self.have_rolledback = True

    def append(self, data):
        """
        Append a chunk to the queue. If the queue is full, the oldest chunk is removed.
        """

        slice_index = [slice(None)]*len(self._buffer.shape)
        slice_index[self._chunk_dim] = slice(self.index,self.index+1)
        self._buffer[tuple(slice_index)] = data

        self.index+=1
        if self.index >= self._max_length:
            self._rollback()
            self.index = self._chunk_length


    def get_top(self):
        """
        Get the top chunk from the queue, return the data and the valid mask
        mask is True for the valid data and False for the padded data
        """        
        
        slice_index = [slice(None)]*len(self._buffer.shape)
        slice_index[self._chunk_dim] = slice(self.index-self._chunk_length, self.index)
        data = self._buffer[tuple(slice_index)]
        if self.have_rolledback: # all valid data
            mask = torch.ones(data.shape[:self._chunk_dim+1], dtype=torch.bool, device=data.device)
        elif self.index < 2*self._chunk_length: # some padded data
            mask = torch.ones(data.shape[:self._chunk_dim+1], dtype=torch.bool, device=data.device)
            mask[..., : 2 * self._chunk_length - self.index] = False
        else: # all valid data
            mask = torch.ones(data.shape[:self._chunk_dim+1], dtype=torch.bool, device=data.device)
        return data, mask


class ChunkBufferBatch:
    def __init__(self, batch_size, data_shape, chunk_length, max_length=None, device=None):
        """
        Create a ChunkBuffer for batch data. The shape of the data should be (B, T, *data_shape)
        This buffer provides reset_idx
        """
        chunk_dim = 1
        shape = [batch_size, 0, *data_shape]
        self.data_buffer = ChunkBuffer(shape, chunk_dim, chunk_length, max_length, dtype=torch.float32, device = device)
        self.mask_buffer = ChunkBuffer(shape[:2], chunk_dim, chunk_length, max_length, dtype=torch.bool, device = device)
        self.mask_buffer._buffer.fill_(False)

    def append(self, data):
        """
        Append a chunk of data to the buffer
         data: (B, *data_shape). No history dimension
        """
        data = data.unsqueeze(1)
        self.data_buffer.append(data)
        mask = torch.ones((data.shape[0], 1), dtype=torch.bool, device=data.device)
        self.mask_buffer.append(mask)
    
    def get_top(self):
        """
        Get the top chunk from the buffer
        """
        data, _ = self.data_buffer.get_top()
        mask, _ = self.mask_buffer.get_top()
        return data, mask

    def reset_idx(self, idx):
        """
        reset the index to chunk_length
        """
        self.mask_buffer._buffer[idx].fill_(False)

class TemporalAggregationBuffer:
    def __init__(self, batch_size, data_shape, chunk_length, max_timesteps, device):
        """
        Initialize the temporal aggregation buffer.
        When appending data, the data writes to buffer[t, t:t+chunk_length].
        When retrieve data, the data reads from buffer[t-chunk_length:t, t-1]. (note always retrieve after appending)
        Args:
            data_shape (tuple): Shape of the data excluding the time dimensions (e.g., (ac_dim)).
            chunk_length (int): Length of the chunks (e.g., chunk_size).
            max_timesteps (int): Maximum number of time steps to store.
            device (torch.device): Torch device to allocate tensors.
        """
        self.batch_size = batch_size
        self.chunk_length = chunk_length
        self.max_timesteps = max_timesteps
        self.device = device

        # Buffer shape: [max_timesteps, max_timesteps + chunk_length, *data_shape]
        self.buffer_shape = [batch_size, max_timesteps, max_timesteps + chunk_length] + list(data_shape)
        self.buffer = torch.zeros(self.buffer_shape, device=device)
        self.mask = torch.zeros([batch_size, max_timesteps, max_timesteps + chunk_length], dtype=torch.bool, device=device)
        self.reset()

    def reset(self):
        """Reset the buffer to its initial state."""
        self.mask.zero_()
        self.buffer.zero_()
        self.timeidx = 0
    
    def reset_idx(self, idx):
        """Reset the buffer to its initial state."""
        self.mask[idx, ...].zero_()

    def append(self, data):
        """
        Append data at the current time step.
        Args:
            data (torch.Tensor): Tensor of shape [batch_size, chunk_length, *data_shape].
        """
        t = self.timeidx
        # Ensure actions have the correct shape
        assert data.shape == tuple([self.batch_size, self.chunk_length] + list(self.buffer.shape[3:])), \
            f"Expected data of shape {[self.batch_size, self.chunk_length] + list(self.buffer.shape[3:])}, got {data.shape}"

        # Store data at position [t, t:t+chunk_length]
        self.buffer[:, t, t:t+self.chunk_length, ...] = data
        self.mask[:, t, t:t+self.chunk_length] = True

        self.timeidx += 1
        if self.timeidx >= self.max_timesteps:
            self._rollback()

    def _rollback(self):
        """
        move the time_idx to chunk_size. And together the buffer[t-chunk_length:t, t-1:t+chunk_length-1] to buffer[0:chunk_length,chunk_length:2*chunk_length]
        """
        self.buffer[:, 
                0 : self.chunk_length, 
                self.chunk_length-1 : 2*self.chunk_length-1, 
            ...] = self.buffer[:, 
                self.timeidx-self.chunk_length : self.timeidx, 
                self.timeidx-1 : self.timeidx+self.chunk_length-1, 
            ...]
        self.mask[:, 
                0 : self.chunk_length, 
                self.chunk_length-1 : 2*self.chunk_length-1
            ] = self.mask[:, 
                self.timeidx-self.chunk_length : self.timeidx, 
                self.timeidx-1 : self.timeidx+self.chunk_length-1
            ]
        self.timeidx = self.chunk_length

    def get_top(self):
        """
        Get the top chunk from the buffer.
        Returns:
            torch.Tensor: Data of shape [batch_size, chunk_length, *data_shape].
            torch.Tensor: Mask of shape [batch_size, chunk_length].
        """
        t = self.timeidx
        slice_dim1 = slice(
            max(t-self.chunk_length, 0), t)
        data = self.buffer[:, slice_dim1, t-1, ...]
        mask = self.mask[:, slice_dim1, t-1]
        return data, mask
