module dut #(
    parameter ROWS = 16,
    parameter COLS = 64,
    parameter LANES = 16,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_x_flat,
    input  wire                    in_x_flat_valid,
    output wire                    in_x_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam int BEATS_A = (ROWS * COLS) / LANES;   // 64
    localparam int BEATS_X = COLS / LANES;            // 4
    localparam int A_BEAT_IDX_BITS = $clog2(BEATS_A); // 6
    localparam int X_ELEM_BITS = $clog2(COLS);        // 6

    typedef enum logic [1:0] { S_IDLE = 2'd0, S_PROC = 2'd1, S_FLUSH = 2'd2 } state_t;
    state_t state_q;

    logic [A_BEAT_IDX_BITS-1:0] a_beat_idx_q;   // counts accepted A beats per txn
    logic [$clog2(ROWS)-1:0]    row_q;          // current row being accumulated
    logic                       x_ready_flag_q; // all x beats received this txn

    logic                       a_fire;
    logic                       x_fire;
    logic                       proc_accept;    // accept an A beat into compute pipeline
    logic                       flush_accept;   // pop a result during flush
    logic                       done_beat;      // last A beat of transaction consumed

    // Independent handshakes for the two input streams.
    assign in_a_flat_ready = (state_q == S_IDLE) ||
                             ((state_q == S_PROC) && proc_accept) ||
                             ((state_q == S_FLUSH) && !done_beat);
    assign in_x_flat_ready = (state_q != S_FLUSH) && !x_ready_flag_q;

    assign a_fire = in_a_flat_valid & in_a_flat_ready;
    assign x_fire = in_x_flat_valid & in_x_flat_ready;

    // ---- Compute pipeline registers ----
    logic signed [DATA_W-1:0] a_reg_q [0:LANES-1];
    logic signed [DATA_W-1:0] x_buf_q [0:COLS-1];
    logic signed [ACC_W-1:0]  acc_q [0:ROWS-1];

    // Result FIFO (depth 4) written by pipeline, read by flush.
    logic signed [ACC_W-1:0] fifo_mem_q [0:3];
    logic [2:0]              fifo_wr_ptr_q, fifo_rd_ptr_q;

    logic                     pipe_vld_q;
    logic signed [ACC_W-1:0]  sum_result_q;

    // Control conditions.
    wire  [5:0]               cur_elem_base = {row_q, 6'd0};
    wire                      row_complete  = (a_beat_idx_q[3:0] == 4'd3); // low 2 bits + beat count parity
    wire                      full_row_done = (a_beat_idx_q[A_BEAT_IDX_BITS-1:0] == BEATS_A[A_BEAT_IDX_BITS-1:0]) ? 1'b1 : 1'b1;

    // We advance to next row when 4 A-beats (COLS/LANES) have been folded into acc[row].
    wire                      fold_last_beat = (a_beat_idx_q[1:0] == 2'd3);

    // Pipeline stage 1: registered multiply-and-add of one lane-group into accumulator.
    logic signed [ACC_W-1:0] adder_sum_w;
    always_comb begin
        adder_sum_w = '0;
        for (int i = 0; i < LANES; i++) begin
            adder_sum_w = adder_sum_w + ($signed(a_reg_q[i]) * $signed(x_buf_q[cur_elem_base + i]));
        end
    end

    wire  pipe_in_vld = proc_accept;
    wire  flush_pop   = (state_q == S_FLUSH) && out_valid && out_ready;

    wire  fifo_push   = pipe_vld_q && (state_q != S_IDLE);
    wire  fifo_full   = ((fifo_wr_ptr_q - fifo_rd_ptr_q) == 3'd4);

    assign out_valid = (state_q == S_FLUSH) && (fifo_rd_ptr_q != fifo_wr_ptr_q);
    assign out_c     = fifo_mem_q[fifo_rd_ptr_q[1:0]];

    // Transaction start/end markers.
    wire txn_start_idle = (state_q == S_IDLE) && (a_fire | x_fire);
    wire go_flush       = (state_q == S_PROC) && done_beat && x_ready_flag_q && !pipe_vld_q &&
                          ((fifo_wr_ptr_q - fifo_rd_ptr_q) == 3'd0 || fifo_full==1'b0);
    // Simpler and correct transition: after the last A beat has been fully folded AND flushed pipeline,
    // move to FLUSH once all rows are produced (they will be, since A had all beats).
    // Detect "all A accepted" via a_beat_idx_q reaching BEATS_A at time of processing that beat.

    integer i;
    initial begin end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state_q         <= S_IDLE;
            a_beat_idx_q    <= '0;
            row_q           <= '0;
            x_ready_flag_q  <= 1'b0;
            pipe_vld_q      <= 1'b0;
            fifo_wr_ptr_q   <= '0;
            fifo_rd_ptr_q   <= '0;
            sum_result_q    <= '0;
            for (i = 0; i < ROWS; i++) acc_q[i] <= '0;
            for (i = 0; i < LANES; i++) a_reg_q[i] <= '0;
        end else begin
            case (state_q)
                S_IDLE: begin
                    pipe_vld_q <= 1'b0;
                    if (a_fire | x_fire) begin
                        // Begin transaction: reset accumulators and pointers.
                        for (i = 0; i < ROWS; i++) acc_q[i] <= '0;
                        a_beat_idx_q   <= '0;
                        row_q          <= '0;
                        x_ready_flag_q <= 1'b0;
                        fifo_wr_ptr_q  <= '0;
                        fifo_rd_ptr_q  <= '0;
                        state_q        <= S_PROC;
                    end
                    // Capture first A beat immediately so computation can proceed without waiting.
                    if (a_fire) begin
                        for (i = 0; i < LANES; i++) a_reg_q[i] <= in_a_flat[i*DATA_W +: DATA_W];
                    end
                    if (x_fire) begin
                        for (i = 0; i < LANES; i++)
                            x_buf_q[x_beat_to_index(a_beat_idx_q_unused, i)] <= in_x_flat[i*DATA_W +: DATA_W];
                    end
                end

                default: ;
            endcase
        end
    end

    function automatic int x_beat_to_index(input int beat, input int lane);
        x_beat_to_index = beat * LANES + lane;
    endfunction

endmodule
