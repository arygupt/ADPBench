module dut #(
    parameter M = 8,
    parameter N = 8,
    parameter K = 16,
    parameter LANES = 16,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_b_flat,
    input  wire                    in_b_flat_valid,
    output wire                    in_b_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    // 8x16 @ 16x8 int8 matmul, exact int32.
    // 16 MACs/cycle (8 columns x 2 k). Row 0 accumulates during input
    // absorb when A[0] is present at B beat 0, so the fast path is
    // 72 cycles/transaction (8 input beats + 64 outputs).
    // Products use a 7x7 unsigned core plus sign correction, which is
    // smaller than a native signed 8x8 and exact on the full int8 range.
    // B banks are pure shift registers (slot 0 is the active k-step).

    localparam S_LOAD  = 2'd0;
    localparam S_RUN   = 2'd1;
    localparam S_DRAIN = 2'd2;

    localparam [3:0] A_BEATS = 4'd8;
    localparam [3:0] B_BEATS = 4'd8;
    localparam [2:0] K_STEPS = 3'd8;
    localparam integer AW = 20;

    reg [1:0] state;
    reg [3:0] a_count;
    reg [3:0] b_count;
    reg [2:0] row_idx;
    reg [2:0] k_step;
    reg       obuf_valid;
    reg [2:0] out_idx;
    reg       row0_on;
    reg       row0_off;
    reg       row0_done;

    reg [K*DATA_W-1:0] A_row [0:M-1];
    reg [N*DATA_W-1:0] Be [0:7];
    reg [N*DATA_W-1:0] Bo [0:7];

    reg signed [AW-1:0] acc  [0:N-1];
    reg signed [AW-1:0] obuf [0:N-1];

    assign in_a_flat_ready = (state == S_LOAD) && (a_count < A_BEATS);
    assign in_b_flat_ready = (state == S_LOAD) && (b_count < B_BEATS);

    wire a_fire = in_a_flat_valid && in_a_flat_ready;
    wire b_fire = in_b_flat_valid && in_b_flat_ready;

    wire a_done = (a_count == A_BEATS) || (a_fire && (a_count == A_BEATS - 4'd1));
    wire b_done = (b_count == B_BEATS) || (b_fire && (b_count == B_BEATS - 4'd1));

    wire a0_avail = (a_count >= 4'd1) || (a_fire && (a_count == 4'd0));
    wire start_row0 = (state == S_LOAD) && !row0_on && !row0_off &&
                      b_fire && (b_count == 4'd0) && a0_avail;
    wire disable_row0 = (state == S_LOAD) && !row0_on && !row0_off &&
                        b_fire && (b_count == 4'd0) && !a0_avail;
    wire load_mac_en = (state == S_LOAD) && b_fire && (start_row0 || row0_on);
    wire row0_completing = load_mac_en && (b_count == 4'd7);
    wire row0_ready = row0_done || row0_completing;

    wire [2:0] step = (state == S_LOAD) ? b_count[2:0] : k_step;
    wire at_row_end = (k_step == K_STEPS - 3'd1);

    wire out_fire = obuf_valid && out_ready;
    wire finishing_obuf = out_fire && (out_idx == 3'd7);
    wire obuf_free_now = !obuf_valid || finishing_obuf;
    wire compute_en = (state == S_RUN) && (!at_row_end || obuf_free_now);
    wire run_capture = compute_en && at_row_end;
    wire capturing = row0_completing || run_capture;

    // One read port. Pair select is an 8:1 of 16 bits, not a 128-bit barrel.
    wire [2:0] a_rd_idx = (state == S_LOAD) ? 3'd0 : row_idx;
    wire [K*DATA_W-1:0] a_row_rd = A_row[a_rd_idx];
    wire [K*DATA_W-1:0] a_src = (state == S_LOAD && a_count == 4'd0) ? in_a_flat : a_row_rd;
    wire [15:0] a_pair =
        (step == 3'd0) ? a_src[15:0] :
        (step == 3'd1) ? a_src[31:16] :
        (step == 3'd2) ? a_src[47:32] :
        (step == 3'd3) ? a_src[63:48] :
        (step == 3'd4) ? a_src[79:64] :
        (step == 3'd5) ? a_src[95:80] :
        (step == 3'd6) ? a_src[111:96] :
                         a_src[127:112];

    wire [6:0] a0_lo = a_pair[6:0];
    wire       a0_sg = a_pair[7];
    wire [6:0] a1_lo = a_pair[14:8];
    wire       a1_sg = a_pair[15];

    // Active B rows. Load shifts new beats in at the top; after 8 beats
    // slot 0 holds k=0/1. RUN circular-shifts the same direction.
    wire [N*DATA_W-1:0] b0_src = (state == S_LOAD) ? in_b_flat[N*DATA_W-1:0] : Be[0];
    wire [N*DATA_W-1:0] b1_src = (state == S_LOAD) ? in_b_flat[LANES*DATA_W-1:N*DATA_W] : Bo[0];

    wire [N*AW-1:0] acc_next_flat;

    genvar gj;
    generate
        for (gj = 0; gj < N; gj = gj + 1) begin : gmac
            wire [7:0] b0 = b0_src[gj*DATA_W +: DATA_W];
            wire [7:0] b1 = b1_src[gj*DATA_W +: DATA_W];
            wire [6:0] b0_lo = b0[6:0];
            wire [6:0] b1_lo = b1[6:0];
            wire       b0_sg = b0[7];
            wire       b1_sg = b1[7];

            // 7x7 unsigned core. a_s = a_lo - 128*a_sg.
            wire [13:0] core0 = a0_lo * b0_lo;
            wire [13:0] core1 = a1_lo * b1_lo;

            wire signed [15:0] p0 = $signed({2'b0, core0})
                - (a0_sg ? $signed({2'b0, b0_lo, 7'b0}) : 16'sd0)
                - (b0_sg ? $signed({2'b0, a0_lo, 7'b0}) : 16'sd0)
                + ((a0_sg & b0_sg) ? 16'sd16384 : 16'sd0);
            wire signed [15:0] p1 = $signed({2'b0, core1})
                - (a1_sg ? $signed({2'b0, b1_lo, 7'b0}) : 16'sd0)
                - (b1_sg ? $signed({2'b0, a1_lo, 7'b0}) : 16'sd0)
                + ((a1_sg & b1_sg) ? 16'sd16384 : 16'sd0);

            wire signed [AW-1:0] psum = {{(AW-16){p0[15]}}, p0} + {{(AW-16){p1[15]}}, p1};
            wire signed [AW-1:0] acc_j = acc[gj];
            wire signed [AW-1:0] anext = (step == 3'd0) ? psum : (acc_j + psum);
            assign acc_next_flat[gj*AW +: AW] = anext;
        end
    endgenerate

    assign out_valid = obuf_valid;
    assign out_c = {{(ACC_W-AW){obuf[0][AW-1]}}, obuf[0]};

    integer ii;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state      <= S_LOAD;
            a_count    <= 4'd0;
            b_count    <= 4'd0;
            row_idx    <= 3'd0;
            k_step     <= 3'd0;
            obuf_valid <= 1'b0;
            out_idx    <= 3'd0;
            row0_on    <= 1'b0;
            row0_off   <= 1'b0;
            row0_done  <= 1'b0;
        end else begin
            if (start_row0)
                row0_on <= 1'b1;
            if (disable_row0)
                row0_off <= 1'b1;

            // B shift. Load and compute are mutually exclusive.
            if (b_fire) begin
                for (ii = 0; ii < 7; ii = ii + 1) begin
                    Be[ii] <= Be[ii+1];
                    Bo[ii] <= Bo[ii+1];
                end
                Be[7] <= in_b_flat[N*DATA_W-1:0];
                Bo[7] <= in_b_flat[LANES*DATA_W-1:N*DATA_W];
            end else if (state == S_RUN && compute_en) begin
                for (ii = 0; ii < 7; ii = ii + 1) begin
                    Be[ii] <= Be[ii+1];
                    Bo[ii] <= Bo[ii+1];
                end
                Be[7] <= Be[0];
                Bo[7] <= Bo[0];
            end

            if (state == S_LOAD) begin
                if (a_fire) begin
                    A_row[a_count[2:0]] <= in_a_flat;
                    a_count <= a_count + 4'd1;
                end
                if (b_fire)
                    b_count <= b_count + 4'd1;
                if (load_mac_en && !row0_completing) begin
                    for (ii = 0; ii < N; ii = ii + 1)
                        acc[ii] <= acc_next_flat[ii*AW +: AW];
                end
                if (a_done && b_done) begin
                    state   <= S_RUN;
                    k_step  <= 3'd0;
                    row_idx <= row0_ready ? 3'd1 : 3'd0;
                end
            end

            if (state == S_RUN && compute_en) begin
                if (!at_row_end) begin
                    for (ii = 0; ii < N; ii = ii + 1)
                        acc[ii] <= acc_next_flat[ii*AW +: AW];
                    k_step <= k_step + 3'd1;
                end else begin
                    k_step <= 3'd0;
                    if (row_idx == 3'd7)
                        state <= S_DRAIN;
                    else
                        row_idx <= row_idx + 3'd1;
                end
            end

            if (capturing) begin
                for (ii = 0; ii < N; ii = ii + 1)
                    obuf[ii] <= acc_next_flat[ii*AW +: AW];
                obuf_valid <= 1'b1;
                out_idx    <= 3'd0;
                if (row0_completing)
                    row0_done <= 1'b1;
            end else if (out_fire) begin
                for (ii = 0; ii < N-1; ii = ii + 1)
                    obuf[ii] <= obuf[ii+1];
                if (out_idx == 3'd7) begin
                    obuf_valid <= 1'b0;
                    out_idx    <= 3'd0;
                    if (state == S_DRAIN) begin
                        state     <= S_LOAD;
                        a_count   <= 4'd0;
                        b_count   <= 4'd0;
                        row_idx   <= 3'd0;
                        k_step    <= 3'd0;
                        row0_on   <= 1'b0;
                        row0_off  <= 1'b0;
                        row0_done <= 1'b0;
                    end
                end else begin
                    out_idx <= out_idx + 3'd1;
                end
            end
        end
    end

endmodule
