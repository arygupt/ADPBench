module dut #(
    parameter IN_LEN = 128,
    parameter F = 4,
    parameter K = 4,
    parameter LANES = 16,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_x_flat,
    input  wire                    in_x_flat_valid,
    output wire                    in_x_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_w_flat,
    input  wire                    in_w_flat_valid,
    output wire                    in_w_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam X_BEATS   = IN_LEN / LANES;         // 8
    localparam X_BEAT_W  = $clog2(X_BEATS);        // 3
    localparam X_CNT_W   = $clog2(X_BEATS + 1);    // 4
    localparam LANE_W    = $clog2(LANES);          // 4
    localparam OUT_POS   = IN_LEN - K + 1;         // 125
    localparam O_W       = $clog2(OUT_POS);        // 7
    localparam F_W       = $clog2(F);              // 2
    localparam W_BITS    = F * K * DATA_W;         // 128
    localparam LANE_EXT_W = $clog2(LANES + K);     // 5

    localparam S_INPUT   = 1'b0;
    localparam S_COMPUTE = 1'b1;

    reg [X_CNT_W-1:0] x_count;
    reg               w_have;
    reg               state;
    reg [F_W-1:0]     f_idx;
    reg [O_W-1:0]     o_idx;

    reg [LANES*DATA_W-1:0] x_word [0:X_BEATS-1];
    reg [W_BITS-1:0]       w_flat;

    // ------------------------------------------------------------
    // Input handshakes (independent ports)
    // ------------------------------------------------------------
    wire x_ready = rst_n && (state == S_INPUT) && (x_count < X_BEATS);
    wire w_ready = rst_n && (state == S_INPUT) && (!w_have);

    assign in_x_flat_ready = x_ready;
    assign in_w_flat_ready = w_ready;

    wire x_inc = in_x_flat_valid && x_ready;
    wire w_inc = in_w_flat_valid && w_ready;

    wire x_full_next = (x_count == X_BEATS) ||
                       ((x_count == (X_BEATS - 1)) && x_inc);
    wire w_full_next = w_have || w_inc;
    wire start_compute = (state == S_INPUT) && x_full_next && w_full_next;

    // ------------------------------------------------------------
    // Transaction / output sequencing
    // ------------------------------------------------------------
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state   <= S_INPUT;
            x_count <= {X_CNT_W{1'b0}};
            w_have  <= 1'b0;
            f_idx   <= {F_W{1'b0}};
            o_idx   <= {O_W{1'b0}};
        end else begin
            case (state)
                S_INPUT: begin
                    if (x_inc) x_count <= x_count + 1'b1;
                    if (w_inc) w_have  <= 1'b1;
                    if (start_compute) state <= S_COMPUTE;
                end

                S_COMPUTE: begin
                    if (out_ready) begin
                        if ((f_idx == (F - 1)) && (o_idx == (OUT_POS - 1))) begin
                            // Transaction complete: clear per-transaction state.
                            state   <= S_INPUT;
                            f_idx   <= {F_W{1'b0}};
                            o_idx   <= {O_W{1'b0}};
                            x_count <= {X_CNT_W{1'b0}};
                            w_have  <= 1'b0;
                        end else if (o_idx == (OUT_POS - 1)) begin
                            f_idx <= f_idx + 1'b1;
                            o_idx <= {O_W{1'b0}};
                        end else begin
                            o_idx <= o_idx + 1'b1;
                        end
                    end
                end
            endcase
        end
    end

    // ------------------------------------------------------------
    // Input storage
    // ------------------------------------------------------------
    always_ff @(posedge clk) begin
        if (x_inc) begin
            x_word[x_count[X_BEAT_W-1:0]] <= in_x_flat;
        end
        if (w_inc) begin
            w_flat <= in_w_flat;
        end
    end

    // ------------------------------------------------------------
    // Convolution read datapath
    // ------------------------------------------------------------
    assign out_valid = (state == S_COMPUTE);

    wire [X_BEAT_W-1:0] o_beat = o_idx[O_W-1 : LANE_W];
    wire [LANE_W-1:0]   o_lane = o_idx[LANE_W-1 : 0];

    wire need_next = (o_lane >= (LANES - K + 1));
    wire [X_BEAT_W-1:0] nxt_beat = need_next ? (o_beat + 1'b1) : o_beat;

    wire [LANES*DATA_W-1:0] cur_word = x_word[o_beat];
    wire [LANES*DATA_W-1:0] nxt_word = x_word[nxt_beat];

    wire [2*LANES*DATA_W-1:0] x_pair = {nxt_word, cur_word};

    wire [LANE_EXT_W-1:0] lane0 = {{(LANE_EXT_W-LANE_W){1'b0}}, o_lane};
    wire [LANE_EXT_W-1:0] lane1 = lane0 + 1'b1;
    wire [LANE_EXT_W-1:0] lane2 = lane0 + 2'd2;
    wire [LANE_EXT_W-1:0] lane3 = lane0 + 2'd3;

    wire [LANE_EXT_W+3-1:0] bit0 = {lane0, 3'b000};
    wire [LANE_EXT_W+3-1:0] bit1 = {lane1, 3'b000};
    wire [LANE_EXT_W+3-1:0] bit2 = {lane2, 3'b000};
    wire [LANE_EXT_W+3-1:0] bit3 = {lane3, 3'b000};

    wire [DATA_W-1:0] x0 = x_pair[bit0 +: DATA_W];
    wire [DATA_W-1:0] x1 = x_pair[bit1 +: DATA_W];
    wire [DATA_W-1:0] x2 = x_pair[bit2 +: DATA_W];
    wire [DATA_W-1:0] x3 = x_pair[bit3 +: DATA_W];

    // w is stored row-major as F*K bytes.
    wire [7:0] w_off = {f_idx, 5'b00000}; // f_idx * K * DATA_W = f_idx*32
    wire [DATA_W-1:0] w0 = w_flat[w_off +: DATA_W];
    wire [DATA_W-1:0] w1 = w_flat[(w_off + 7'd8) +: DATA_W];
    wire [DATA_W-1:0] w2 = w_flat[(w_off + 7'd16) +: DATA_W];
    wire [DATA_W-1:0] w3 = w_flat[(w_off + 7'd24) +: DATA_W];

    // Int8 x int8 products are exact in 16 bits.  Use signed 8-bit operands
    // so synthesis infers small multipliers.
    wire signed [15:0] p0 = $signed(x0) * $signed(w0);
    wire signed [15:0] p1 = $signed(x1) * $signed(w1);
    wire signed [15:0] p2 = $signed(x2) * $signed(w2);
    wire signed [15:0] p3 = $signed(x3) * $signed(w3);

    wire signed [31:0] s0 = $signed({{16{p0[15]}}, p0});
    wire signed [31:0] s1 = $signed({{16{p1[15]}}, p1});
    wire signed [31:0] s2 = $signed({{16{p2[15]}}, p2});
    wire signed [31:0] s3 = $signed({{16{p3[15]}}, p3});

    assign out_c = s0 + s1 + s2 + s3;

endmodule
